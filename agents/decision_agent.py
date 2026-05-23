"""AI Decision Agent: analyzes market state, validates output, outputs BUY/SELL/HOLD recommendations."""

import asyncio
from datetime import datetime, timezone
from typing import Literal, Optional
import anthropic

from config.settings import settings
from logging.logger import get_logger
from db.models import AIDecision
from db.session import async_session_factory

log = get_logger(__name__)

Recommendation = Literal["BUY", "SELL", "HOLD"]

# Minimum confidence below which we auto-reject (treat as HOLD)
MIN_CONFIDENCE = 0.65


SYSTEM_PROMPT = f"""You are an expert crypto trading analyst. You analyze real-time market data, technical indicators, and price action to make binary buy/sell/hold decisions.

Output format — return EXACTLY this JSON with no preamble:
{{"action": "BUY", "confidence": 0.XX, "reasoning": "one sentence"}}

Rules:
- BUY: price is likely to rise within the next 15-60 minutes. Requires alignment across 3+ indicators OR strong momentum signal.
- SELL: price is likely to fall OR current position should be closed. Risk-reward favors exiting.
- HOLD: No clear edge. Market is neutral, choppy, or indicators are conflicting.
- Never fabricate data. If indicators are missing or insufficient, output HOLD with 0.50 confidence.
- Minimum confidence threshold: if your confidence is below {MIN_CONFIDENCE}, you MUST output HOLD."""


class AIDecisionAgent:
    """
    The AI agent that receives market data and outputs a structured recommendation.
    It NEVER executes trades. It only decides and logs.

    Safety features:
    - LLM output schema enforcement via Anthropic response_format
    - Minimum confidence threshold (below {MIN_CONFIDENCE} → auto-HOLD)
    - Empty indicators detection → auto-HOLD
    - Parse error handling → auto-HOLD
    - Decision throttling (no more than 1 decision per 60 seconds)
    """

    def __init__(self):
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._lock = asyncio.Lock()
        self._last_decision_at: Optional[datetime] = None
        self._last_decision_price: Optional[float] = None
        self._min_decision_interval_seconds = 60

    async def analyze_and_decide(self, market_snapshot: dict) -> dict:
        """
        Main entry point. Takes market snapshot from market_data_engine.
        Returns a dict with action, confidence, and reasoning.
        """
        async with self._lock:
            now = datetime.now(timezone.utc)

            # Throttle: minimum interval between decisions
            if self._last_decision_at:
                elapsed = (now - self._last_decision_at).total_seconds()
                if elapsed < self._min_decision_interval_seconds:
                    log.debug("decision_throttled", seconds_elapsed=elapsed)
                    return {"action": "HOLD", "confidence": 0.5, "reasoning": "throttled", "throttled": True}

            snapshot = market_snapshot
            indicators = snapshot.get("indicators", {})

            # ── Skip LLM call if price hasn't moved enough ─────────────────────
            price_delta_threshold_pct = 0.5
            if self._last_decision_price:
                price_pct_change = abs(
                    (snapshot["price"] - self._last_decision_price) / self._last_decision_price * 100
                )
                if price_pct_change < price_delta_threshold_pct:
                    log.debug("decision_skipped_price_unchanged", price_change_pct=price_pct_change)
                    return {
                        "action": "HOLD",
                        "confidence": 0.5,
                        "reasoning": f"price unchanged ({price_pct_change:.2f}%)",
                        "throttled": True,
                    }

            # ── Build prompt and call LLM ───────────────────────────────────────
            prompt = self._build_prompt(snapshot, indicators)
            raw_response = await self._call_llm(prompt)
            decision = self._parse_and_validate(raw_response, indicators)

            # ── Persist decision to DB ───────────────────────────────────────────
            decision_record = AIDecision(
                symbol=snapshot["symbol"],
                action=decision["action"],
                confidence=decision["confidence"],
                reasoning=decision["reasoning"],
                market_state=self._summarize_market(snapshot),
                indicators_snapshot=str(indicators),
            )
            async with async_session_factory() as session:
                session.add(decision_record)
                await session.commit()
                decision["decision_id"] = decision_record.id

            self._last_decision_at = now
            self._last_decision_price = snapshot["price"]

            log.info(
                "ai_decision_made",
                symbol=snapshot["symbol"],
                action=decision["action"],
                confidence=decision["confidence"],
                price=snapshot["price"],
            )
            return decision

    def _build_prompt(self, snapshot: dict, indicators: dict) -> str:
        indicators_text = "\n".join([f"- {k}: {v:.4f}" for k, v in indicators.items()])
        return f"""Analyze this market snapshot and decide.

Symbol: {snapshot['symbol']}
Current Price: ${snapshot['price']:.4f}
24h Momentum: {snapshot.get('momentum_pct', 0):.3f}%
Volatility: {snapshot.get('volatility_pct', 0):.3f}%
Spread: {snapshot.get('spread_pct', 0):.4f}%

Technical Indicators:
{indicators_text}

Return ONLY the JSON object."""

    async def _call_llm(self, prompt: str) -> str:
        try:
            response = self._client.messages.create(
                model=settings.ai_model,
                max_tokens=512,
                temperature=0.2,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                # Schema enforcement: encourages valid JSON output
                response_format={"type": "json_object"},
            )
            return response.content[0].text.strip()
        except Exception as e:
            log.error("ai_llm_error", error=str(e))
            return '{"action": "HOLD", "confidence": 0.5, "reasoning": "llm_error"}'

    def _parse_and_validate(self, raw: str, indicators: dict) -> dict:
        """
        Parse LLM response and validate:
        - Valid JSON
        - Valid action
        - Confidence >= MIN_CONFIDENCE
        - Indicators not empty
        """
        import json, re

        # Strip markdown code fences
        raw = re.sub(r"```json\s*", "", raw)
        raw = re.sub(r"```\s*", "", raw).strip()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            log.error("ai_parse_error", raw=raw[:200])
            return {"action": "HOLD", "confidence": 0.5, "reasoning": "parse_error"}

        # Validate action
        action = parsed.get("action", "").upper()
        if action not in ("BUY", "SELL", "HOLD"):
            log.error("ai_invalid_action", action=parsed.get("action"))
            return {"action": "HOLD", "confidence": 0.5, "reasoning": "invalid_action"}

        confidence = float(parsed.get("confidence", 0.0))

        # Validate confidence
        if confidence < MIN_CONFIDENCE:
            log.info("ai_confidence_below_threshold", confidence=confidence, min=MIN_CONFIDENCE)
            return {
                "action": "HOLD",
                "confidence": confidence,
                "reasoning": f"confidence {confidence:.2f} below threshold {MIN_CONFIDENCE}",
            }

        # Validate indicators not empty
        if not indicators or all(v is None or (isinstance(v, float) and v == 0) for v in indicators.values()):
            log.warning("ai_indicators_empty")
            return {
                "action": "HOLD",
                "confidence": confidence,
                "reasoning": "insufficient market data / indicators unavailable",
            }

        return {
            "action": action,
            "confidence": confidence,
            "reasoning": str(parsed.get("reasoning", ""))[:200],
        }

    def _summarize_market(self, snapshot: dict) -> str:
        ind = snapshot.get("indicators", {})
        return (
            f"price={snapshot['price']:.4f} | "
            f"rsi={ind.get('rsi_14', 0):.1f} | "
            f"macd={ind.get('macd', 0):.4f} | "
            f"ema9={ind.get('ema_9', 0):.4f} | "
            f"adx={ind.get('adx', 0):.1f} | "
            f"momentum={snapshot.get('momentum_pct', 0):.3f}%"
        )


ai_decision_agent = AIDecisionAgent()