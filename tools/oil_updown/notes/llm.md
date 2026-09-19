# LLM-based candidates for WTI daily direction (Polymarket "WTI Up or Down")

Compiled 2026-09-14. Read-only search: nothing was downloaded, installed, or run.
Target: sign of the WTI front-month change from 5pm ET to 5pm ET. Every candidate must be backtested only on dates **after** its knowledge or pretraining cutoff.

How facts were checked: model cards, READMEs, arXiv full text or abstracts, and official model-card pages. "Abstract only" or "unverified" means I did not confirm it in the full text. † means the base-model cutoff comes from general knowledge and was not re-checked.

Bottom line: no source tests an LLM on **next-day WTI direction on data from after the model's cutoff**, so our backtest would be new evidence. The only verified daily-WTI direction result is CrudeBERT at 52.5% over 2012–2021, with no held-out split. The only verified weekly result is Dai et al.: AUC 0.65, with GPT-4o's cutoff falling inside the test window.

---

## 1. Shortlist: most worth testing

| # | Candidate | Type | Why test it | Main risk |
|---|---|---|---|---|
| 1 | **CrudeBERT** (known; new facts in §2) | Oil-headline sentiment encoder, 110M | Only verified **daily** WTI direction result (52.5% on 3,376 days). Cheap; runs on CPU. Training data ends in 2021, so a test from 2022 on is clean. | Result is in-sample; edge is thin |
| 2 | **Dai et al. recipe moved to a daily horizon** (known) | GPT-4o or local-LLM scores on 5 sentiment dimensions + FinBERT, fed to LightGBM | Best verified out-of-sample numbers on WTI (weekly AUC 0.65). Prompt is published, so any local LLM can stand in for GPT-4o. | GPT-4o cutoff (Oct 2023) is inside their test window; weekly only |
| 3 | **"Bullish or bearish for WTI?" prompt on a small local LLM with a verified cutoff** (Gemma 4 E2B, Olmo 3 7B, Phi-4-mini) | Zero-shot headline judge feeding a daily aggregate | Fixes the polarity problem of finance encoders ("OPEC cuts output" reads negative but is bullish for crude). Clean test window from 2025 onward. | No published evidence; prompt sensitivity |
| 4 | **Chronos-2** (120M, Apache-2.0) | Time-series foundation model with covariates | Can take Brent, dollar index, gasoline futures and curve spread at decision time. Quantile output gives P(up). Runs on M2. | No oil result; stock direction ~50% |
| 5 | **EXAONE Finance 1.0** (202M) | Finance time-series foundation model | Pretrained on **synthetic data only**, so no price-history leakage. #1 on FinVerse, which includes commodity futures. | Non-commercial license; FinVerse is its authors' own benchmark and scores ≥1-week horizons |
| 6 | **TiRex** (35M) / **TimesFM 2.5** (231M) | Time-series foundation models | Best directional hit rates on FinVerse among general models; tiny; fast on M2 | Pretraining mixes may include finance series (GIFT-Eval subsets) |
| 7 | **Kronos** (4M–102M, MIT) | Candlestick (OHLCV) foundation model | Takes daily OHLCV bars directly; sample paths give P(up); pretraining data **ends Jun 2024** | Oil instruments in its 75 Chinese futures unverified; no commodity results |
| 8 | **Point-in-time LLMs as a leakage check** (PIT-4B-FT-202412, DatedGPT-2024) | Vintage LLMs | Run the same headline prompt on a leak-free vintage and a modern model. A big gap on pre-cutoff dates signals memorization. | Much weaker models |

Worth a look but not first in line: FinGPT sentiment v3.3 (13B; too big for 8 GB at Q8), a report-day OPEC/IEA sentiment flag (Jeong & Ahn 2025), Zhou et al. SSRN 5750745 (claims daily out-of-sample gains on Chinese crude; abstract only), and the agent-forecaster approach validated against market price (TimeSeek).

---

## 2. Oil and commodity papers using LLMs or NLP

| Name + link | LLM role | Target / horizon | Inputs at decision time | Code / weights | Cutoff vs test | Reported out-of-sample result | Red flags |
|---|---|---|---|---|---|---|---|
| **Kaplan et al., "Unifying Economic and Language Models…Oil Market"** [arXiv 2410.12473](https://arxiv.org/abs/2410.12473) (extended CrudeBERT; *new facts on a known item*) | CrudeBERT (FinBERT fine-tuned with supply/demand domain adaptation), GPT-3.5 prompts, RavenPack ESS: all sentiment scorers | **Next-day WTI futures direction** (binary) | Oil headlines (RavenPack, ~26.5k, 2012–2021), prior-day sentiment | Weights at [HF Captain-1337/CrudeBERT](https://hf.co/Captain-1337/CrudeBERT) (no license tag); [thesis repo](https://github.com/Captain-1337/Master-Thesis) | GPT-3.5 training data covers the whole test | 2012-01-01 to 2021-04-01, 3,376 days: CrudeBERT 1,774 correct (52.5%) vs RavenPack 1,721, GPT-3.5 ~51.5%, FinBERT 1,643 (below random). Chi-square tests: vs FinBERT significant at p<0.05, vs RavenPack at p<0.10 | Scored over the full period, no held-out split; classes balanced 1688/1688 (looks resampled); headline timing vs close unclear; no costs |
| **Dai et al.** [arXiv 2603.11408](https://arxiv.org/abs/2603.11408) (*known; new facts*) | GPT-4o scores 5 sentiment dimensions + FinBERT → LightGBM | Weekly WTI return direction | Weekly news aggregates | Prompt in paper | GPT-4o cutoff Oct 2023 is inside the 2020–2025 test | Accuracy 0.49–0.58, AUC ≤0.65 over ~313 weeks; FinBERT adds ~+0.05 AUC; Llama-3.2-3B alone AUC 0.589 | 52.9% of weeks were up, and no always-long baseline is reported; look-ahead before Oct 2023 |
| **Jeong & Ahn 2025**, *Energy Economics* 141:108105 ([RePEc](https://ideas.repec.org/a/eee/eneeco/v141y2025ics0140988324008144.html)) | ChatGPT scores sentiment in OPEC MOMR and IEA OMR reports | Future oil returns (horizon unverified; reports are monthly) | OPEC and IEA monthly reports | Paywalled | Unverified | Sentiment predicts **lower** future returns; OPEC matters more than IEA; certainty-equivalent gain +2.40% to +2.56% | Abstract only; low frequency |
| **Zhou et al.**, SSRN [5750745](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5750745) | Chain-of-thought LLM extracts factors from news, aggregated daily | Chinese crude futures price, 1/2/5-day horizons | News | Unverified | Unverified | Abstract claims out-of-sample gains at all horizons | **Abstract only**; model and dates unknown. Get the full text before relying on it |
| **Gao, Wang, Wang & Zhang 2025**, *J. Futures Markets* 45(3) ([DOI](https://doi.org/10.1002/fut.22568)) | ChatGPT labels articles rise / fall / unclear → PLS index | **Monthly** excess return of an equal-weighted commodity futures portfolio (18 GSCI commodities) | 2.5M newspaper articles | No | 1946–2022 sample overlaps any cutoff | In-sample adj. R² 0.83% at 1 month (2.29% for 2004–2022); out-of-sample numbers not verified | Monthly; look-ahead; overlapping returns |
| **Wang et al., "Macro Economists in the Machine"** [arXiv 2606.08283](https://arxiv.org/abs/2606.08283) | gpt-4o-mini as Hawkish, Dovish and Debate agents | Weekly tilts across 15 commodity ETFs | 7 FRED z-scores (VIX, USD, fed funds, industrial production, breakevens, real yield, unemployment); no news | Available on request only | Cutoff Oct 2023; test Oct 2023 to Feb 2026 (mostly clean) | 124 weeks: Sharpe 0.57 vs 0.53 rule-based; p=0.067 unadjusted; hit rate 54.8–55.7% vs 56.5% buy-and-hold | Thin edge; one rate cycle; not oil-specific |
| **Paredes Amorin et al., aluminum** [arXiv 2603.09085](https://arxiv.org/abs/2603.09085) | Fine-tuned Qwen3-8B sentiment vs FinBERT; zero-shot Qwen3-8B topic/event tagging | Monthly SHFE aluminum long-short | Reuters, Dow Jones and China News headlines + macro | Not stated | Qwen3 (2025) covers 2007–2024 test | LSTM with sentiment Sharpe 1.04 vs 0.23 without; headlines about events that already happened 0.62 vs forward-looking commentary −0.01 | Topic mix chosen from 4,094 combinations on the same data. **Worth borrowing:** keep "already happened" headlines, drop speculation |
| **Ghali et al.** [arXiv 2508.06497](https://arxiv.org/abs/2508.06497) | LLM agents write yearly news summaries from memory | Yearly >25% commodity price spike | LLM-recalled summaries | None | Model recalls the very years it predicts | AUC 0.94 on 64 annual points | **Severe leakage**; skip |
| **Liu & Huang, AGESL** [arXiv 2111.09111](https://arxiv.org/abs/2111.09111) | Pre-LLM event extraction + VADER + ARIMA-GARCH-LSTM | Next-day WTI **price level** | Guardian/NYT oil news + prices | None | n/a | RMSE 1.08; "DS" 0.689 on an 80/20 split of 2007–2020 | Level target; direction score uses the realized next price; skip |
| **Hashami & Maldonado** [arXiv 2508.20707](https://arxiv.org/abs/2508.20707) (known) | News-based model | Brent **volatility** direction | News | — | — | — | Volatility, not price direction |
| FuturesMind ([GitHub](https://github.com/2779639552/FuturesMind)), TradingAgents fork | DeepSeek debate agents | Reports on 21 Chinese commodity futures | Chinese social sentiment + AKShare prices | Apache-2.0 | n/a | No accuracy published | Scaffold only |
| Unverified leads (snippet only) | — | Procedia CS 2025 "Prediction of Crude Oil Price using LLM"; IEEE Access 2026 LLM + graph oil paper; MPP-GPT for methanol (*Energy* 2025); AI-GPR geopolitical risk index built with GPT-4o-mini (possible feature) | — | — | — | — | All level or low-frequency targets as far as seen |

---

## 3. Hugging Face finance LLMs and forecasters (none trained on oil)

| Name + link | Output | Horizon | Inputs | Weights / license / size / GGUF | Base + cutoff | Out-of-sample result | Red flags |
|---|---|---|---|---|---|---|---|
| [FinGPT/fingpt-sentiment_llama2-13b_lora](https://hf.co/FinGPT/fingpt-sentiment_llama2-13b_lora) (v3.3) | neg/neu/pos | n/a | Headline + fixed prompt | LoRA, MIT, 13B; third-party Q8 GGUF (unverified) | Llama-2-13B, Sep 2022† | F1 FPB 0.882, FiQA 0.874, TFNS 0.903, NWGI 0.643 | Company-view polarity; too big for 8 GB |
| FinGPT v3.1 ChatGLM2 / [v3.2 Llama2-7B](https://hf.co/oliverwang15/FinGPT_v32_Llama2_Sentiment_Instruction_LoRA_FT) / [mt_llama3-8b](https://hf.co/FinGPT/fingpt-mt_llama3-8b_lora); [GGUF](https://hf.co/second-state/FinGPT-MT-Llama-3-8B-LoRA-GGUF) | neg/neu/pos | n/a | Headline | LoRAs; 6–8B; Llama-3 GGUF 4.9 GB | Llama-3-8B: "March 2023" | v3.2 F1 FPB 0.850, TFNS 0.894 | Cards empty; mt_llama3 sample code is broken |
| [FinGPT v1.1 market-feedback LoRA](https://hf.co/oliverwang15/FinGPT_v11_Llama2_13B_Sentiment_Market_Feedback_LoRA_FT_8bit) | 7-class labels from the **following 5-day price change** | 5 days | Headline | LoRA 8-bit, 13B | Llama-2; train 2019–2021, test Jan 2022–Aug 2023 | 3-class accuracy 41.35% | US stocks. **Right labeling design to copy for oil:** label by the price move that followed, split train/test by time |
| [karthikeyanvijayan/LFM2-1.2B-FinGPT-Sentiment-RL](https://hf.co/karthikeyanvijayan/LFM2-1.2B-FinGPT-Sentiment-RL) ([GGUF](https://hf.co/mradermacher/LFM2-1.2B-FinGPT-Sentiment-RL-GGUF), 0.8 GB) | Sentiment (inferred from name) | n/a | Headline | No license, no card | LFM2-1.2B, cutoff not stated | None | Empty card |
| [korra141/fingpt-forecaster-llama3-lora](https://hf.co/korra141/fingpt-forecaster-llama3-lora) | Up/down + % + rationale | 1 week | Company news, financial ratios, prices | LoRA, 8B | Llama-3-8B; Dow30 May 2023–May 2024 | Direction accuracy 0.612 on **50** samples | Random 80/20 split; stocks |
| FinGPT forecaster forks ([sz50](https://hf.co/FinGPT/fingpt-forecaster_sz50_llama2-7B_lora), [qwen3](https://hf.co/tuananhle/fingpt-forecaster_dow30_qwen3-8b_lora_250814_v3), [nasdaq100](https://hf.co/SkylineYang/fingpt-forecaster_nasdaq100_23-24_llama2-7b_lora)) | Weekly stock direction | 1 week | Same | LoRAs | Various | None | Stocks; boilerplate cards |
| [suryo12/fingpt-crypto-v5](https://hf.co/suryo12/fingpt-crypto-v5) | JSON: conviction, LONG/SHORT/SKIP per horizon | 1h/4h/24h | Headline + price, funding rate, volatility regime | Q8 GGUF 8.7 GB + Ollama Modelfile; Apache-2.0 | Qwen3-8B LoRA | Validation loss only | Crypto; no accuracy test. Useful **build template** |
| [ChanceFocus/finma-7b-full](https://hf.co/ChanceFocus/finma-7b-full) (PIXIU) | Rise/fall text | Next day | Tweets + prices | Gated, MIT, 7B | LLaMA-1 | Accuracy 49–51% on ACL18/BigData22/CIKM18 | Near chance; stocks |
| Open-FinLLMs / FinLLaMA ([arXiv 2408.11878](https://arxiv.org/abs/2408.11878)) | Movement | Next day | Tweets + prices | Weights claimed on HF but **unverified** | Llama-3-8B + finance data to 2023 | Accuracy 0.53–0.56 | Backtest overlaps pretraining |
| [TheFinAI/StockLLM](https://hf.co/TheFinAI/StockLLM) + [FinSeer](https://hf.co/TheFinAI/FinSeer) | Rise/fall (±0.5%) | Next day | 5-day prices + retrieved sequences | Gated, non-commercial, 1B | Llama-3.2-1B, Dec 2023 | Accuracy 0.51–0.54, MCC 0.02–0.09 | Near chance; stocks |
| [SUFE-AIFLM-Lab/Fin-R1](https://hf.co/SUFE-AIFLM-Lab/Fin-R1) ([GGUF](https://hf.co/bartowski/SUFE-AIFLM-Lab_Fin-R1-GGUF) Q4 4.7 GB) | Reasoning text | n/a | Prompt | Apache-2.0, 7.6B | Qwen2.5-7B-Instruct; cutoff not stated; released Mar 2025 | FinQA 76, ConvFinQA 85 | Q&A only, no forecasting; mostly Chinese data |
| [TheFinAI/Fin-o1-8B](https://hf.co/TheFinAI/Fin-o1-8B), [Josephgflowers/FinR1-llama-8b](https://hf.co/Josephgflowers/FinR1-llama-8b-multi-language-thinking), DianJin-R1, CFGPT2, XuanYuan | Reasoning / chat | n/a | Prompt | 7–70B | Qwen3 / Llama-3.1 (Dec 2023) / Qwen2.5 / InternLM2 / Llama2 | No market tests | Finance Q&A only |
| FinDPO ([arXiv 2507.18417](https://arxiv.org/abs/2507.18417)), Trading-R1 ([arXiv 2509.11420](https://arxiv.org/abs/2509.11420)) | Sentiment score / trade decision | Daily | Headline / equity data | **No weights released** | Llama-3-8B / Qwen3-4B | FinDPO S&P 500 backtest Sharpe 2.0 | Backtest before cutoff; unreleased |
| [nyanko1999/oil-price-model](https://hf.co/nyanko1999/oil-price-model) (known) | — | — | — | Empty repo, created Aug 2026; duplicate `oil-price-mode` also exists | — | — | Nothing to test |

---

## 4. Sentiment, topic and event encoders for oil headlines

| Name + link | Output | Size / license | Training data | Reported result | Red flags for oil |
|---|---|---|---|---|---|
| [Captain-1337/CrudeBERT](https://hf.co/Captain-1337/CrudeBERT) (known) | Oil-specific pos/neg/neutral | ~110M; no license tag | FinBERT + oil supply/demand adaptation, headlines 2012–2021 | 52.5% next-day WTI direction (§2) | In-sample result |
| [ProsusAI/finbert](https://hf.co/ProsusAI/finbert) | pos/neg/neu softmax | ~110M; no license on card | Financial PhraseBank (FPB) | ~0.88 on FPB (inflated) | Company-view polarity; below random on next-day WTI (Kaplan) |
| [yiyanghkust/finbert-tone](https://hf.co/yiyanghkust/finbert-tone) | neutral/pos/neg | BERT; no license on card | 4.9B tokens of filings, earnings calls, analyst reports | — | Analyst-report tone |
| [AnkitAI/FinSense-ModernBERT](https://hf.co/AnkitAI/FinSense-ModernBERT-Financial-News-Sentiment-Analysis) | pos/neu/neg | 149M; Apache-2.0 | FPB | Held-out accuracy 0.868, macro-F1 0.859 | FPB-only |
| [mrm8488 deberta-v3](https://hf.co/mrm8488/deberta-v3-ft-financial-news-sentiment-analysis), [distilroberta](https://hf.co/mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis), [FinancialBERT-SA](https://hf.co/ahmedrachid/FinancialBERT-Sentiment-Analysis), [Sigma](https://hf.co/Sigma/financial-sentiment-analysis) | pos/neu/neg | 82–142M | FPB | 0.98–0.99 (inflated) | Random FPB splits on the easy all-annotators-agree subset |
| [Jean-Baptiste/roberta-large-financial-news-sentiment-en](https://hf.co/Jean-Baptiste/roberta-large-financial-news-sentiment-en) | neg/neu/pos | ~355M; MIT | FPB + 2k Canadian news | F1 93% overall, 84% on Canadian news | Mining/Canadian company tilt |
| [StephanAkkerman/FinTwitBERT-sentiment](https://hf.co/StephanAkkerman/FinTwitBERT-sentiment) | Sentiment | MIT | 10M financial tweets | — | Social text, not newswire |
| [QuantBridge/energy-news-classifier-ner-multitask](https://hf.co/QuantBridge/energy-news-classifier-ner-multitask) | 19 BIO entity tags (COMMODITY, ORG e.g. OPEC+, LOCATION e.g. Hormuz, MARKET e.g. WTI/Brent, EVENT) + 10 topics | ~67M DistilBERT; Apache-2.0 | AG News / Reuters / Kaggle | 86 entities on 40 headlines (no F1) | No sentiment. Use as an **event and relevance filter** only |
| [QuantBridge/energy-news-classifier](https://hf.co/QuantBridge/energy-news-classifier) | 10 topic labels | DistilBERT; Apache-2.0 | Unstated | None | Topic only |
| [Durrani95/eurobert-geopolitical-multiclass](https://hf.co/Durrani95/eurobert-geopolitical-multiclass) | 11 geopolitical topics | 210M | — | — | Europe-focused |
| [FinLang/finance-embeddings-investopedia](https://hf.co/FinLang/finance-embeddings-investopedia) | 768-d embeddings | cc-by-nc-4.0 | bge-base + Investopedia | — | Non-commercial |
| [nlpaueb/sec-bert-base](https://hf.co/nlpaueb/sec-bert-base) | Fill-mask only | cc-by-sa-4.0 | 10-K filings | — | Needs fine-tuning |
| [islem04/finbert-commodity-sentiment](https://hf.co/islem04/finbert-commodity-sentiment) | Unknown | Unknown | Unknown | — | Empty card; skip |
| Data: [polibert/oil-sentiment-headlines](https://hf.co/datasets/polibert/oil-sentiment-headlines) (known, 18,450 headlines); [newsdata01/Global-Energy-and-Oil-Market-News-Dataset](https://hf.co/datasets/newsdata01/Global-Energy-and-Oil-Market-News-Dataset) (one-time snapshot, MIT) | — | — | — | — | Possible fine-tuning or eval data; check dates |

Main problem with this whole group: every finance encoder scores sentiment from a company's point of view, not the oil price's. Either (a) have an LLM restate each headline as bullish or bearish for WTI, or (b) fine-tune on oil headlines labeled by the next 5pm-to-5pm WTI move with a time-based split (FinGPT v1.1 design).

---

## 5. Time-series foundation models and LLM-as-forecaster methods

Decision-time input for all: the last ≤512–2048 daily closes; Chronos-2 and EXAONE also take covariates. Direction = compare the 1-step median or P(up) with the last close. "Dir.acc" = directional accuracy on daily stock excess returns, 2001–2023, from [arXiv 2511.18578](https://arxiv.org/abs/2511.18578). "HR" = FinVerse directional hit rate at ≥1-week horizons ([arXiv 2608.03259](https://arxiv.org/abs/2608.03259)). No model has a published daily oil direction result.

| Model + link | Output | Weights / license / size / 8 GB M2? | Pretraining data + cutoff | Finance / commodity results | Red flags |
|---|---|---|---|---|---|
| [Chronos-2](https://hf.co/amazon/chronos-2) | 21 quantiles; multivariate + covariates; context 8192 | Apache-2.0; 120M; yes | Chronos corpus subset + GIFT-Eval pretraining subset + synthetic; released Oct 2025 | FinVerse #5 overall; stocks/Treasuries RMSE only ([2605.21504](https://arxiv.org/abs/2605.21504)) | Share of finance data in GIFT-Eval unverified |
| [Chronos-T5](https://hf.co/amazon/chronos-t5-small) / [Chronos-Bolt](https://hf.co/amazon/chronos-bolt-small) | Sample paths / 9 quantiles | Apache-2.0; 8–710M; yes | Public corpora incl. M4 + synthetic; Mar / Nov 2024 | Dir.acc 48.3–51.0%; Bolt HR 0.645 | Most tests predate the cutoff |
| [TimesFM 2.5](https://hf.co/google/timesfm-2.5-200m-pytorch) | Point + quantiles; 16k context | Apache-2.0; 231M; yes | GIFT-Eval pretraining + Wikipedia + Google Trends + synthetic; Sep 2025 | Dir.acc 48.4–50.3%; FinVerse #4, HR 0.653 | — |
| [TiRex](https://hf.co/NX-AI/TiRex) | Quantiles 0.1–0.9 | NX-AI custom license; 35M; yes | Chronos data + GIFT-Eval subset + synthetic; May 2025 | Dir.acc 48.6–50.6%; FinVerse #3, HR 0.666 | Check license |
| [Toto](https://hf.co/Datadog/Toto-Open-Base-1.0) | Samples | Apache-2.0; 151M; yes | 2.36T points (43% Datadog telemetry, ~33% synthetic); May 2025 | Best zero-shot stock dir.acc 50.7%; **unstable on crude oil realized volatility** ([2607.05291](https://arxiv.org/abs/2607.05291)) | Contamination flagged by the authors |
| [EXAONE Finance 1.0](https://huggingface.co/LG-AI-Research/EXAONE-Finance-1.0) ([arXiv 2609.04239](https://arxiv.org/abs/2609.04239)) | 21 quantiles; context 512; multivariate | "exaone" custom license (non-commercial per report); 202M; yes | **Synthetic financial corpus only** (card confirms); Sep 2026 | #1 on all FinVerse tiers, which include commodity futures | Benchmark built by the same team; ≥1-week horizons |
| [Kronos](https://hf.co/NeoQuasar/Kronos-small) | Sampled OHLCV candle paths | MIT; 4.1 / 24.7 / 102M; yes | 12.1B candles (stocks, crypto, FX, 75 Chinese futures); **data ends Jun 2024** | Stocks/crypto/FX correlation (IC) scores, test from Jul 2024; no commodity results | Oil-linked instruments in pretraining unverified; start test Jul 2024 or later |
| [FinCast](https://hf.co/Vincent05R/FinCast) ([2508.19609](https://arxiv.org/abs/2508.19609)) | Point + quantiles | Apache-2.0; 1B sparse mixture-of-experts; borderline | 20B points incl. 1.71B futures points; **sources and dates undisclosed** | Normalized-level MSE only | Possible leakage |
| [Sundial](https://hf.co/thuml/sundial-base-128m) | Samples | Apache-2.0; 128M; yes | TimeBench incl. 10.5B undisclosed "Finance" points | Dir.acc 48.5–50.5%; FinVerse #32 | Possible leakage |
| [Moirai 1.1 / 2.0](https://hf.co/Salesforce/moirai-2.0-R-small) | Samples / quantiles | **CC-BY-NC-4.0**; 11–311M; yes | LOTSA (0.1% econ/finance) | Dir.acc 48.2–50.4%; HR 0.53–0.57 | Non-commercial; weak |
| [Lag-Llama](https://hf.co/time-series-foundation-models/Lag-Llama) | Student-t samples | Apache-2.0; 2.4M; yes | 27 datasets, no finance, ends ~2021 | Dir.acc 48.5–51.4% | Weak |
| [Time-MoE](https://hf.co/Maple728/TimeMoE-200M) | Point | Apache-2.0; 50M / 200M active; yes | Time-300B (finance ≈0.0001%); Sep 2024 | USDA crop prices MAE (level metric) | — |
| [Timer](https://hf.co/thuml/timer-base-84m) | Point | Apache-2.0; 84M; yes | UTSD + LOTSA | None | — |
| TimeGPT (Nixtla API) | Point + intervals | Closed, paid | ">100B points" incl. finance; no dataset list or cutoff | None on oil | **Leakage can't be checked**; model can change server-side |
| [FinText](https://hf.co/FinText) (2511.18578) | Chronos/TimesFM architectures trained from scratch | Apache-2.0; 8–46M; yes | Stock excess returns only; **one model per year 2000–2023** | ~50.2–51.4% dir.acc | Clean yearly cutoffs, but no commodities |
| [LLMTime](https://github.com/ngruver/llmtime) | 20 samples; prices written as digits in text | MIT code; GPT-3/4 API or LLaMA-2-70B; no for M2 | Backbone's corpus | Level metrics on Bitcoin / FRED-MD | Biggest memorization risk |
| [ChatTime](https://github.com/ForestsKing/ChatTime) | Point, zero-shot | LLaMA-2-7B; weights on HF; marginal for M2 | LLaMA-2 cutoff (~Sep 2022†) | Exchange-rate runs only | — |
| Time-LLM, AutoTimes, GPT4TS/OFA, UniTime, LLM4TS, CALF, TEMPO, TimeCMA, PromptCast | Point, 96–720 steps, **trained per dataset** | GPT-2 / Llama-7B backbones | — | Normalized-level MSE; none on oil | Tan et al. 2024 ([2406.16964](https://arxiv.org/abs/2406.16964)): removing the LLM matches or beats Time-LLM in 26/26 cases. Several repos drop the last test batch; TimeCMA keeps the best-on-test checkpoint |
| News + time-series: From News to Forecast ([2409.17515](https://arxiv.org/abs/2409.17515)), CompEvo ([2609.09195](https://arxiv.org/abs/2609.09195)), [2606.03097](https://arxiv.org/abs/2606.03097), Time-MMD ([2406.08627](https://arxiv.org/abs/2406.08627)), ScenarioDiff ([2608.17164](https://arxiv.org/abs/2608.17164)), LAFP ([2607.24892](https://arxiv.org/abs/2607.24892)), Nexus ([2605.14389](https://arxiv.org/abs/2605.14389)) | Point | Llama-2/3.1, DeepSeek, GPT-4 | — | Closest to oil is Time-MMD's "Energy" = **weekly US retail gasoline** (EIA) + report text; MSE only | Test windows predate the LLM cutoffs; no crude |

---

## 6. Small local LLMs (8 GB M2) with verified cutoffs

"Fits" means the Q4 file fits in the ~5.3 GB of GPU memory an 8 GB M2 gives by default: Y = fits, T = tight. Safe start = first month after the stated cutoff (add a 1–3 month buffer). Strict start = day after public release (later fine-tuning can add newer facts).

| Model | Sizes | Q4 size / fits | ollama tag | License | Knowledge cutoff (source wording) | Release | Safe start / strict start |
|---|---|---|---|---|---|---|---|
| **Gemma 4** E2B / E4B ([card](https://ai.google.dev/gemma/docs/core/model_card_4)) | 2.3B effective (5.1B total) / 4.5B (8B) | QAT 3.35–4.3 GB Y / 5.2–6.1 GB T | `gemma4:e2b-it-qat`, `e4b-it-qat` | Apache-2.0 | "with a cutoff date of January 2025" (re-checked) | 2026-04-02 | 2025-02 / 2026-04-03 |
| **Olmo 3 7B** Instruct/Think ([HF](https://hf.co/allenai/Olmo-3-7B-Instruct)) | 7B | 4.5 GB Y | `olmo-3:7b-instruct` | Apache-2.0 | "Date cutoff: Dec. 2024." Training data is public, so it can be searched for oil news | 2025-11-20 | 2025-01 / 2025-11-21 |
| **Phi-4-mini-instruct** ([HF](https://hf.co/microsoft/Phi-4-mini-instruct)) | 3.8B | 2.5 GB Y | `phi4-mini` | MIT | "cutoff date of June 2024 for publicly available data" | 2025-02 | 2024-07 / 2025-03 |
| Phi-4-mini-reasoning | 3.8B | 3.2 GB Y | `phi4-mini-reasoning` | MIT | "cutoff date of February 2025" | 2025-04 | 2025-03 / 2025-05 (math-tuned) |
| Gemma 3 1B / 4B | 1B / 4B | 0.8 / 3.3 GB (QAT 4.0) Y | `gemma3:4b-it-qat` | Gemma Terms | "August 2024" | 2025-03-12 | 2024-09 / 2025-03-13 |
| Gemma 3n E2B / E4B | ~5B / ~8B raw | 5.6 / 7.5 GB T/N | `gemma3n:e2b` | Gemma Terms | "June 2024" | 2025-06-26 | 2024-07 / 2025-06-27 |
| Nemotron 3 Nano 4B | 4B | 2.8 GB Y | `nemotron-3-nano:4b` | NVIDIA Open Model | Pretraining "cutoff date of September 2024"; post-trained Dec 2025–Jan 2026 | ~2026-03 | use 2026-02 or later |
| SmolLM3-3B | 3B | 1.9 GB Y | `hf.co/ggml-org/SmolLM3-3B-GGUF` | Apache-2.0 | Chat template: "Knowledge Cutoff Date: June 2025" | 2025-07-08 | 2025-07 / 2025-07-09 |
| Llama 3.2 1B / 3B | 1B / 3B | 0.8 / 2.0 GB Y | `llama3.2:3b` | Llama 3.2 Community | "cutoff of December 2023" | 2024-09-25 | 2024-01 / 2024-09-26 |
| Llama 3.1 8B | 8B | 4.9 GB T | `llama3.1:8b` | Llama 3.1 Community | "December 2023" | 2024-07-23 | 2024-01 / 2024-07-24 |
| OLMo 2 7B | 7B | 4.5 GB Y | `olmo2:7b` | Apache-2.0 | "Date cutoff: Dec. 2023." | 2024-11-26 | 2024-01 / 2024-11-27 |
| LFM2.5-1.2B / 350M | 1.2B | 0.7 GB Y | HF GGUF | LFM Open License | "Knowledge cutoff: Mid-2024" (vague) | 2026-01-05 | 2025-01 / 2026-01-06 |
| Qwen3 0.6–8B, Qwen3-2507 4B, Qwen3.5 0.8/2/4B, Qwen2.5 | — | 0.5–5.2 GB Y/T | `qwen3:4b-instruct-2507-q4_K_M`, `qwen3.5:4b` | Apache-2.0 | **Not stated** | 2024-09 / 2025-04 / 2025-08 / ~2026-03 | Release date only |
| DeepSeek-R1-Distill-Qwen 1.5B/7B, R1-0528-Qwen3-8B | — | 1.1–5.2 GB | `deepseek-r1:7b`, `:8b` | MIT | Not stated | 2025-01 / 2025-05 | Release date only |
| Ministral 3 3B/8B, IBM Granite 3.3/4.x, LFM2 | — | 2–6 GB | `ministral-3:3b`, `granite4.2` | Apache-2.0 / LFM | Not stated | 2025-04 to 2026-08 | Release date only |
| gpt-oss-20b | 21B (3.6B active) | 14 GB **N** | `gpt-oss:20b` | Apache-2.0 | "knowledge cutoff of June 2024" | 2025-08-05 | Too big for 8 GB |
| Finance fine-tunes: Fin-R1 7.6B (Q4 4.7 GB T), FinR1-llama-8b (4.9 GB T), FinGPT-MT-Llama-3-8B (4.9 GB T), LFM2-1.2B-FinGPT-Sentiment-RL (0.8 GB Y) | — | — | — | — | Bases: Qwen2.5 (not stated) / Llama-3.1 (Dec 2023) / Llama-3 (Mar 2023) / LFM2 (not stated) | 2025-03 / 2025-10 / 2024-10 / 2025-09 | Use release date: fine-tune data dates are unknown |

### Point-in-time (vintage) LLMs, for leakage checks

| Name + link | Sizes | Vintages | License | Notes |
|---|---|---|---|---|
| **PIT** (Kelly, Malamud, Schwab, Xu) [hf.co/Diamegs](https://hf.co/Diamegs), [arXiv 2607.11889](https://arxiv.org/abs/2607.11889) | PIT-1B (1.5B), PIT-4B (4.2B), PIT-4B-FT instruct | Yearly Dec 2013 – Dec 2024 (+2015-11); trained on monthly FineWeb snapshots | Apache-2.0 | Custom architecture needs remote code; no GGUF. Repos confirmed on HF |
| **DatedGPT** [hf.co/datedgpt](https://hf.co/datedgpt), [arXiv 2603.11838](https://arxiv.org/abs/2603.11838) | 1.3B Llama architecture, base + instruct, 2k context | Yearly 2013–2024 | Not tagged (paper CC BY 4.0) | Repos confirmed; likely convertible to GGUF |
| ChronoGPT-Instruct (known; new fact) [hf.co/manelalab](https://hf.co/manelalab) | ChronoGPT size | Year-end 1999–2024; instruct v1 posted Oct 2025 | MIT | Still no 2025 vintage |
| TiMaGPT [hf.co/Ti-Ma](https://hf.co/Ti-Ma), [arXiv 2404.18543](https://arxiv.org/abs/2404.18543) | GPT-2 small | 2011–2022 | CC0 | Too old |
| Look-ahead diagnostics (no models): Lopez-Lira et al. "Memorization Problem" [2504.14765](https://arxiv.org/abs/2504.14765); "Detecting Lookahead Bias in LLM Forecasts" [2512.23847](https://arxiv.org/abs/2512.23847); FinCAD decoding fix [2605.24564](https://arxiv.org/abs/2605.24564); Look-Ahead-Bench [2601.13770](https://arxiv.org/abs/2601.13770) | — | — | — | Recall test: ask each model for WTI settlement prices on dates before and after its cutoff |

---

## 7. Evaluation and methodology references (not signals)

| Name + link | What it adds |
|---|---|
| **TimeSeek** [arXiv 2604.04220](https://arxiv.org/abs/2604.04220) ([GitHub](https://github.com/coys17/timeseek-anonymous)) | 10 frontier LLM agents on 150 Kalshi markets that resolved after their cutoffs (Oct 2025–Jan 2026). All lost to the market price overall (best Brier skill −0.068, Claude Opus 4.5). The "Financial" category was negative for all 10, and all were worse near resolution. Contrarian calls on ~50/50 markets won 80.9% (n=68). Implication: score any LLM by **Brier skill against the Polymarket price**, not raw accuracy. Kalshi also lists daily WTI markets. |
| **Hindcast** [arXiv 2607.14051](https://arxiv.org/abs/2607.14051) | Leakage-free replay on 216 resolved Polymarket markets using a frozen archive cut off per market. Retrieval lowered Brier for 8 of 9 open models. Released pipeline is a template for our replay. |
| **OpenPM** [arXiv 2608.09988](https://arxiv.org/abs/2608.09988) ([GitHub](https://github.com/aslcai/OpenPM-Bench)) | Timestamp gate on every input record plus a per-run contamination certificate. Method to copy. |
| **Yao & Zheng** [arXiv 2606.08285](https://arxiv.org/abs/2606.08285) | Audit of 30 LLM trading papers (timing, costs, splits are under-reported). Use as a reporting checklist. |
| TradingAgents [2412.20138](https://arxiv.org/abs/2412.20138), FinMem [2311.13743](https://arxiv.org/abs/2311.13743), FinAgent [2402.18485](https://arxiv.org/abs/2402.18485), FinCon [2407.06567](https://arxiv.org/abs/2407.06567) | **None applied to commodities or futures.** Short test windows before GPT-4's cutoff; best settings chosen on the test period. Code for TradingAgents and FinMem only. |
| Ntale [arXiv 2607.15414](https://arxiv.org/abs/2607.15414) | Tables labeled "Simulated / Illustrative Data"; equities only. **Do not use.** |
| Zhu et al. [arXiv 2607.28496](https://arxiv.org/abs/2607.28496) | LLaMA-3.1-70B structured news extraction for stocks; AUROC 0.528; random bootstrap splits leak. Only the extraction schema (event type, horizon, confidence) is reusable. |

---

## 8. Recurring red flags and test rules

1. **Cutoff overlap:** nearly every published LLM result (GPT-3.5/4/4o, Llama, Qwen) is tested inside the model's training period. Only use post-cutoff windows (§6 start dates).
2. **Wrong target:** most time-series results are MSE/MASE on normalized price levels or ≥1-week hit rates. Nobody reports 5pm-to-5pm daily direction; rebuild labels on 5pm ET prints (all studies used settlement or close).
3. **Missing baselines:** always report always-up, yesterday's direction, and the Polymarket price itself (Brier skill), not just 50%.
4. **Polarity:** finance encoders are company-centric; oil needs supply/demand-aware scoring (CrudeBERT, LLM restatement, or labels from the next day's WTI move).
5. **Inflated encoder scores:** FPB fine-tunes near 0.99 come from random splits of the easy subset.
6. **Opaque pretraining:** FinCast, Sundial and TimeGPT may already contain futures price history over the test window; deprioritize them.
7. **Headline timing:** only headlines timestamped before the decision time (before 5pm ET, or before entry) may be used. Kaplan et al. are unclear on this.
