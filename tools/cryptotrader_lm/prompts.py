"""Prompt renderers and decision parser for CryptoTrader-LM (stdlib only).

The team never published their training prompt, so two reconstructions are
rendered and always labeled:
- "finmem": the organizers' FinMem test-mode prompt (the harness that produced
  the model's only official score), strings copied verbatim from
  felis33/coling-cryptocurrency-trading-challenge src/chat/prompt/vllm_prompt.py,
  including its missing separators.
- "teacher": the team's paper Appendix B prompt (ACL 2025.finnlp-1.48), turned
  from "explain the known correct decision" into "make the decision".
This module is also copied into the GPU job, so it must not import the repo.
"""
from __future__ import annotations

import json
import re

TICKER = {"btc": "BTC", "eth": "ETH"}
COIN = {"btc": "Bitcoin", "eth": "Ethereum"}

WRAPPERS = {
    "card": "[INST]{prompt}[/INST]",
    "tutorial": "[INST] {prompt} [/INST]",
    "system": "[INST]You are a helpful assistant.\n\n{prompt}[/INST]",
}

_FINMEM_PREFIX = "The ticker of the cryptocurrency to be analyzed is {symbol} and the current date is {cur_date}"
_FINMEM_SENTIMENT = (
    'In investment decision-making, analyzing financial sentiment is pivotal, offering insights into market perceptions and forecasting potential market trends. Sentiment analysis divides market opinions into three categories: positive, negative, and neutral. A positive sentiment signals optimism about future prospects, often leading to increased buying activity, while negative sentiment reflects pessimism, \n'
    '                        likely causing selling pressures. Neutral sentiment indicates either uncertainty or a balanced view, suggesting that investors are neither overly bullish nor bearish. Leveraging these sentiment indicators enables investors and analysts to fine-tune their strategies to better match the prevailing market atmosphere.\n'
    "                        Additionally, news about competitors can significantly impact a company's cryptocurrency price. For example, if a competitor unveils a groundbreaking product, it may lead to a decline in the cryptocurrency prices of other companies within the same industry as investors anticipate potential market share losses.\n"
    '                        '
)
_FINMEM_MOMENTUM = (
    'The information below provides a summary of cryptocurrency price fluctuations over the previous few days, which is the "Momentum" of a cryptocurrency.\n'
    '        It reflects the trend of a cryptocurrency.\n'
    '        Momentum is based on the idea that securities that have performed well in the past will continue to perform well, and conversely, securities that have performed poorly will continue to perform poorly.\n'
    '        '
)
_FINMEM_MOMENTUM_SENTENCE = {
    -1: "The cumulative return of past 3 days for this cryptocurrency is negative.",
    1: "The cumulative return of past 3 days for this cryptocurrency is positive.",
}
_FINMEM_FINAL = (
    'Given the information, can you make an investment decision? Just summarize the reason of the decision.\n'
    '    please consider the mid-term information, the long-term information, the reflection-term information only when they are available. If there no such information, directly ignore the impact for absence such information.\n'
    '    please consider the available short-term information and sentiment associated with them.\n'
    '    please consider the momentum of the historical cryptocurrency price.\n'
    '    When momentum or cumulative return is positive, you are a risk-seeking investor. \n'
    "    In particular, you should choose to 'buy' when the overall sentiment is positive or momentum/cumulative return is positive.\n"
    '    please consider how much shares of the cryptocurrency the investor holds now.\n'
    '    You should provide exactly one of the following investment decisions: buy or sell.\n'
    "    When it is very hard to make a 'buy'-or-'sell' decision, then you could go with 'hold' option.\n"
    '    You also need to provide the ids of the information to support your decision.'
)

_TEACHER = """You are tasked with making a trading decision (buy, hold, or sell) for a particular day based on cryptocurrency-related news, historical price momentum, and investor sentiment. Your goal is to make the decision and provide clear reasoning for it. Just summarize the reason of the decision.

Consider the following:

1. **Short-term Information and Sentiment**: Focus on the short-term crypto news and market sentiment. Was the news positive or negative? How did it affect market sentiment in the short term?

2. **Mid-term and Long-term Information**: If mid-term or long-term information is available (such as regulatory changes, major partnerships, or technological advancements), consider its relevance. If no such information is available, ignore the impact of its absence.

3. **Historical Momentum**: Analyze the historical price momentum of the cryptocurrency. Was the price trend positive or negative in the days leading up to the decision? How did this momentum influence the decision?

### Your Task:
Make the trading decision (buy, hold, or sell) and provide reasoning for it by analyzing the following:

- The short-term impact of the news and sentiment.
- The mid-term and long-term information, if available.
- The cryptocurrency's historical momentum and cumulative return.

Your reasoning should clearly explain why **this particular decision (buy, hold, or sell)** is the most appropriate based on the available information.

Additionally, for each point in your reasoning, provide the **IDs of the information** that support your decision, but strictly do not just repeat the news, you can only make judgemets.

News: {news}

Make reasoning for the coin: {coin}

Ticker symbol: {ticker}

Current price: {price}

Price momentum for previous 30 days: {price_momentum}"""


def news_item(title: str, sentiment: str) -> str:
    return f"{title.strip().lower()} (sentiment:{sentiment})"


def finmem_prompt(asset: str, cur_date: str, news: list[tuple[str, str]],
                  momentum: int | None) -> str:
    info = _FINMEM_PREFIX.format(symbol=f"{TICKER[asset]}-USD", cur_date=cur_date)
    if news:
        info += "The short-term information:\n"
        info += "\n".join(f"{i}. {news_item(t, s)}" for i, (t, s) in enumerate(news, 1))
        info += _FINMEM_SENTIMENT
        info += "\n\n"
    if momentum:
        info += _FINMEM_MOMENTUM + _FINMEM_MOMENTUM_SENTENCE[momentum]
    return info + _FINMEM_FINAL


def teacher_prompt(asset: str, news: list[tuple[str, str]], price: float,
                   daily_returns_30d: list[float] | None) -> str:
    items = [f"{i}. {news_item(t, s)}" for i, (t, s) in enumerate(news, 1)]
    momentum = "unavailable" if daily_returns_30d is None else json.dumps(
        [f"{r:+.2f}%" for r in daily_returns_30d])
    return _TEACHER.format(news=json.dumps(items, ensure_ascii=False, indent=0),
                           coin=COIN[asset], ticker=TICKER[asset], price=f"{price:.2f}",
                           price_momentum=momentum)


def wrap(prompt: str, wrapper: str) -> str:
    return WRAPPERS[wrapper].format(prompt=prompt)


_CHOICE = r"['\"*`\s]*(buy|sell|hold)\b"
_EXPLICIT = [
    re.compile(r'"?investment_decision"?\s*[:=]' + _CHOICE, re.I),
    re.compile(r"\bdecision\b[*:\s-]*(?:(?:is|would be|will be|to|should be)\s+)*" + _CHOICE, re.I),
    re.compile(r"\b(?:recommend|choose|decide|decision to)\s+(?:to\s+)?" + _CHOICE, re.I),
    re.compile(r"\*\*\s*(buy|sell|hold)\s*\*\*", re.I),
]
_WORD = re.compile(r"\b(buy|sell|hold)\b", re.I)


def parse_decision(text: str) -> tuple[str | None, str]:
    """(decision, method). method is 'explicit', 'single_word' or 'none'."""
    for pattern in _EXPLICIT:
        m = pattern.search(text)
        if m:
            return m.group(1).lower(), "explicit"
    words = {w.lower() for w in _WORD.findall(text)}
    if len(words) == 1:
        return words.pop(), "single_word"
    return None, "none"
