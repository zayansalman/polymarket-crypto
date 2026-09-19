# WTI daily direction: econometric / statistical / rule-based candidates

Research date: 2026-09-14. Read-only literature + source check. Nothing was backtested.
Target: Polymarket daily "WTI Up or Down" = sign(today's close − prior trading day's close) of the *Active Month* CL future.

Evidence labels used below:
- **[FT]** = I read the paper's own text or tables.
- **[ABS]** = I only saw the abstract or a publisher/IDEAS summary. Numbers are as the abstract states them and were not checked against tables.
- **[3P]** = a third-party summary or replication.

Already known and not repeated: Wen et al. 2021 (intraday momentum, USO), Wen et al. 2023 (EIA-day half-hour), Ye & Karali 2016, Ferraro-Rogoff-Rossi 2015, Brandt & Gao 2019, HAR vol-direction ~65-71%.

---

## 0. Resolution mechanics that change what counts as a "predictor" (verified)

| Fact | Source | Why it matters |
|---|---|---|
| The resolution price is the Pyth 1-minute candle "Close" for the Active Month CL at "the final minute of regular trading hours". The backup source is CME. An exact tie resolves 50-50. | [Polymarket rules](https://polymarket.com/event/wti-up-or-down-on-august-3-2026) | Labels must come from the same contract and time. FRED `DCOILWTICO` is Cushing *spot*, not the futures close. Yahoo `CL=F` daily "close" may be the 14:30 settlement. Both are proxies with label noise. |
| Pyth publishes a separate feed per contract (e.g. `WTIV6` = Oct-2026, expiring 22 Sep 2026). The schedule is `0000-1700 & 1800-2400` America/New_York. So the "close" minute is 16:59-17:00 ET, which matches the user's 5pm definition. Holiday sessions close early (e.g. 14:30 on some dates). | [Pyth benchmarks API](https://benchmarks.pyth.network/v1/price_feeds/?query=WTI&asset_type=commodities) (responded) | Everything released before 17:00 ET falls *inside* that day's window: the API report (Tue 16:30), COT (Fri 15:30), Baker Hughes (Fri ~13:00), EIA (Wed 10:30). Asia/Europe session moves are also *part of* the day's return, not predictors of it. |
| Active Month "changes at the start of the second trading session prior to the nearest listed contract's last trading session". CL last trade is 3 business days before the 25th. | Polymarket rules (CME convention) | **Must verify before any modelling.** On the roll session, is "prior close" taken from the *old* contract or the *new* one? If it is the old one, the M2−M1 calendar spread enters the label mechanically: contango pushes toward Up, backwardation toward Down. This happens about 12 times a year, and a $1-2 spread on $100 oil is 1-2%, versus a ~2-3% daily sd. That would be the largest single structural effect in this catalog. |
| EIA WPSR comes out Wed after 10:30 ET. In holiday weeks it moves to Thu 12:00 ET. 2026 dates: 22 Jan, 19 Feb, 28 May, 10 Sep, 15 Oct, 12 Nov. | [EIA schedule](https://www.eia.gov/petroleum/supply/weekly/schedule.php) (verified) | Any "Wednesday effect" test should use the actual release day, not the weekday. |

---

## 1. Benchmarks and theory: the floor every model must beat

### 1.1 Random walk / no-change
- **Ellwanger & Snudden (2023, JBF)**, [link](https://www.sciencedirect.com/science/article/pii/S0378426623001619), and **"Point forecasts of the price of crude oil: beat the end-of-month RW" (Empirical Economics 2024)**, [link](https://ideas.repec.org/a/spr/empeco/v67y2024i4d10.1007_s00181-024-02599-8.html). [ABS]
  - Finding: the end-of-period no-change forecast beats most published monthly models. Many earlier "wins" came from comparing against a monthly-*average* no-change forecast.
  - Red flag for any paper that averages prices over a period.
  - Horizon is monthly. The lesson carries over: use the end-of-day close as the label and the benchmark.
- **Baumeister & Kilian (2015, JBES)**, forecast combination, [link](https://ideas.repec.org/p/bca/bocawp/13-28.html). [ABS]
  - Real-time combination of 6 models. MSPE up to 18% below no-change. Directional accuracy up to 77%.
  - Horizon 1-24 months, not daily.
  - **Garratt, Vahey & Zhang (2019, JAE)** [link](https://onlinelibrary.wiley.com/doi/abs/10.1002/jae.2673) extends this real-time approach.
  - Red flag if carried to daily: the monthly predictors (inventories, real activity) do not update daily.
- **Degiannakis & Filis (2018, Energy Econ.)**, "high-frequency financial data are indeed useful". Daily financial data feed a MIDAS model for monthly oil. Relevant only as a source of predictors. Not re-verified.

### 1.2 Why "volatility predicts sign" is tiny at a daily horizon
- **Christoffersen & Diebold (2006, Mgmt Sci)** [link](https://pubsonline.informs.org/doi/10.1287/mnsc.1060.0520), and **Christoffersen, Diebold, Mariano, Tay & Tse (2007)** [pdf](https://www.sas.upenn.edu/~fdiebold/papers/paper73/CDMTT.pdf). [ABS]
  - Idea: if the expected return μ is not zero, volatility dynamics make the sign predictable.
  - Rough scale for WTI: μ ≈ 0.02%/day, σ ≈ 2.5%. P(up) ≈ 0.5 + φ(0)·μ/σ ≈ **50.3%**.
  - So GARCH/HAR vol forecasts cannot give a useful daily *direction* edge alone. They are useful as the σ in 1.3.
  - The known HAR 65-71% figure is about *volatility* direction, not price direction.

### 1.3 Partial-session conditional probability, if Polymarket can be traded during the day
- Model: P(Up | return so far r_t, remaining vol σ_rem) ≈ Φ(r_t / σ_rem), assuming no drift.
  - σ_rem comes from an intraday vol curve scaled by HAR-RV or OVX.
  - Example: +1% at 10:30 ET with σ_rem ≈ 1.8% gives Φ(0.56) ≈ 71%.
- This is the statistical baseline any intraday trade has to beat. The edge would be Polymarket odds lagging Φ, not beating the random walk.
- Inputs: live CL price, the prior 17:00 close, and an intraday seasonal vol profile (EIA Wed 10:30 spike, 14:30 settlement, 16:30 API on Tuesday).
- Code: none needed beyond scipy/numpy. The vol forecast can use `arch` (HARX) or statsmodels.
- Held up? It is a pricing identity, not an anomaly. The known Wen et al. intraday momentum result adds a small drift term.

---

## 2. Calendar and scheduled-event effects

### 2.1 Day-of-week / Wednesday (EIA) effect
- **Li, Zhu, Wen & Nor (2022, Energy Econ. 106:105817)**, [IDEAS](https://ideas.repec.org/a/eee/eneeco/v106y2022ics014098832200007x.html). [ABS]
  - Target: WTI daily return. Sample 14 May 2007 – 14 May 2021, with a time-varying (evolving) DOW test.
  - Finding: **abnormal positive return on Wednesdays**, which they tie to the EIA inventory schedule. The negative Monday return "disappears sometimes".
  - Inputs: a weekday dummy, ideally the actual EIA release-day dummy.
  - OOS: none reported (in-sample event/dummy tests).
  - Held up? The sample includes 2020-May 2021. Nothing after mid-2021.
  - Red flags: in-sample only; DOW effects have many possible specifications (multiple testing).
- **"Day-of-the-week effect: Petroleum and petroleum products" (Cogent Econ. & Finance, 2023)**, [link](https://www.tandfonline.com/doi/full/10.1080/23322039.2023.2213876). [ABS]
  - WTI, Brent, RBOB, HO, NG futures. DOW dummies in the mean equation with asymmetric GARCH variants.
  - Effects vary by commodity. Full text was blocked (403), so the day-level numbers are not verified.
- **Hoelscher, Mbanga & Nelson, "TGIF? The Weekend Effect in Energy Commodities" (J. Finance Issues)**, [pdf](https://jfi-aof.org/index.php/jfi/article/download/2264/1847). [FT abstract]
  - EIA daily *spot* WTI 1986–May 2017, Brent 1987–2017. Robust OLS plus median regression, full sample and 3 subperiods.
  - Finding: weekend effect present for WTI and Brent (a reverse effect for natural gas).
  - Red flags: spot, not futures; ends 2017.
- **Qadan, Aharon & Eichel (2019, Resources Policy)**, [link](https://www.sciencedirect.com/science/article/abs/pii/S0301420719300133). [ABS, via search summaries]
  - Negative Monday; Thursday then Friday highest; weak Nov-Dec.
  - Note that calendar anomalies "disappear in recent years".
- **Yue, Li & Wu (2025, IRFA)**, "Weekday variations in the Chinese crude oil futures market: COVID-19 and EIA shocks", [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4869649). [ABS]
  - INE SC. Significant positive Monday effect *before* COVID; insignificant over the full sample.
  - EIA × Thursday interaction significantly positive (Thursday Asia = the session after the US Wednesday release).
  - Supports the idea that EIA day carries a return premium that spills into the next session.
- **Analogue: Prokopczuk, Wese Simen & Wichmann (2021, Energy J.)**, "Natural Gas Announcement Day Puzzle", [IDEAS](https://ideas.repec.org/a/aen/journl/ej42-2-wichmann.html). [ABS]
  - More than 50% of natural gas futures' annual return is earned on EIA storage days. The surprise does not explain it. About half comes before the release.
  - A simple strategy is profitable after costs.
  - Not oil, but it is the same mechanism as Li et al.'s WTI Wednesday premium, so it is a direct template for the test.
- **Test later:** `ret_t ~ EIAday_t + Mon + Fri + holiday-shift` with HAC standard errors. Report the hit rate of "long on EIA day" by year for 2007-2026. Code: statsmodels OLS/Logit.

### 2.2 API Tuesday (16:30 ET, inside the Tuesday window) and pre-EIA informed trading
- **Ederington, Lin, Linn & Yang (2019, Energy J. 40(5))**, "EIA Storage Announcements, Analyst Storage Forecasts, and Energy Prices", [IDEAS](https://ideas.repec.org/a/aen/journl/ej40-5-ederington.html). [ABS]
  - Prices react to analyst-forecast surprises *before* EIA.
  - Crude analyst forecasts do *not* efficiently use time-series information (natural gas forecasts do).
  - **Storage surprises partially reverse the next week.** That makes next week's surprise sign partly predictable.
  - Red flag: analyst consensus data (Bloomberg/Reuters polls) is not free.
- **Rousse & Sévi (2019, Energy J. 40(2))**, "Informed trading in the WTI oil futures market", [IDEAS](https://ideas.repec.org/a/aen/journl/ej40-2-rousse.html). [ABS]
  - On bearish (higher-than-forecast) inventory days: abnormal order flow in the 2 hours before release, average price drift of **−0.25%** before the news, and an over-reaction partly reversed afterwards.
  - Needs tick data. Probably sampled in the early 2010s (not verified).
- **Kurov, Sancetta, Strasser & Wolfe (2019, JFQA)**, "Price drift before U.S. macro news", [IDEAS](https://ideas.repec.org/p/boc/bocoec/881.html); follow-up **"Drift Begone!" (2022)**, [pdf](https://www.skidmore.edu/economics/documents/Kurov-Sancetta-Wolfe-2022-Drift-Begone.pdf). [ABS]
  - 2008-2014: 9 of 20 announcements show pre-announcement drift starting ~30 minutes before, about 40% of the total move.
  - The 2022 paper studies release-policy changes (tighter lock-ups) that *removed* drift for some announcements.
  - Red flag: pre-2015 effects may already be gone.
- **Halova, Kurov & Kucher (2014, JFM)**, "Noisy inventory announcements and energy prices", [link](https://onlinelibrary.wiley.com/doi/10.1002/fut.21633). [ABS]
  - Returns respond more to crude inventory surprises in the injection season than the withdrawal season. Measurement error biases naive surprise betas.
- **Practitioner claim:** API and EIA agree on direction "about 80%" of the time ([CME OpenMarkets 2025](https://www.cmegroup.com/openmarkets/energy/2025/What-API-and-EIA-Data-Reveal-About-Crude-Oil-Markets.html)). Not peer-reviewed; unverified.
- **Test later:** (a) the Tuesday 16:30-17:00 move after API (inside the Tuesday label) versus the Wednesday label; (b) whether API sign predicts the EIA-day sign. Data problem: the API bulletin is paid. Headline numbers appear in news. The EIA actual is free.

### 2.3 OPEC / OPEC+ decision days
- **Demirer & Kutan (2010, Energy Econ.)**, OPEC and SPR announcements, [link](https://www.sciencedirect.com/science/article/abs/pii/S0140988310001027). [ABS]
  - 1983-2008. Production *cut* announcements give significant positive abnormal returns, smaller at longer maturities. Increases: no significant effect.
- **Lin & Tamvakis (2010, Energy Policy)**, [IDEAS](https://ideas.repec.org/a/eee/enepol/v38y2010i2p1010-1016.html). [ABS]
  - 1982-2008. Responses depend on the price band.
- **Loutia, Mellios & Andriosopoulos (2016, Energy Policy 90)**, [IDEAS](https://ideas.repec.org/a/eee/enepol/v90y2016icp262-272.html). [ABS]
  - Q1 1991 – Q1 2015, EGARCH event study.
  - Effects vary over time, are strongest for *cut* and *maintain* decisions, differ between WTI and Brent, and are sensitive to the benchmark.
- **Schmidbauer & Rösch (2012, Energy Econ.)**, [link](https://www.sciencedirect.com/science/article/abs/pii/S0140988312000072). [ABS]
  - Implied vol drifts up into meetings, then drops ~3% after day 1 and ~5% over 5 days. A vol effect, not direction.
- **Verdad (Oct 2022, practitioner)**, [link](https://verdadcap.com/archive/the-impact-of-opec-announcements-on-oil-prices). [3P]
  - Since 2015: cuts gave roughly **+5%**, but it **faded within the following week** (1991-2015 effects lasted ≥5 days). Increases: no significant effect. "Maintain": negative cumulative abnormal return.
  - This is the closest thing to recent (OPEC+ era) evidence found.
- **Känzig (2021, AER)**, oil supply news shocks from high-frequency futures moves around OPEC announcements. Data: [github.com/dkaenzig/oilsupplynews](https://github.com/dkaenzig/oilsupplynews) (responded; daily surprise series plus announcement dates); replication code in Matlab.
  - Used to identify shocks, not to predict returns. Its value here is a clean list of dates and surprise signs.
- **Held up?** The impact is real but decays faster after 2015. The direction on the day depends on the decision being a surprise, which is not known before the event.
- **Test use:** a regime/volatility flag on meeting days, plus a "post-cut fade" rule (day+1..+5 reversal).
- Red flags: roughly 8-12 events a year, many held on weekends (the move shows up in Monday's label). Effects conditional on the outcome are not usable before the fact.

### 2.4 Roll / expiry-week effects
- **Mou (2010)**, "Front-Running the Goldman Roll", [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1716841). [ABS]
  - GSCI roll (5th-9th business day) price impact. Calendar-spread strategies earned Sharpe up to **4.39** over 2000-Mar 2010.
  - The effect is in the M1-M2 *spread*, not the outright front-month sign.
  - Red flag: pre-2010. Later work finds costs have shrunk: **Irwin, Sanders & Yan (2022)**, "Order flow cost of index rolling", [pdf](https://scotthirwin.com/wp-content/uploads/2022/02/Irwin_Sanders_Yan_AEPP_All.pdf), and **Maréchal**, "Commodity index funds: the price impact of the roll", [pdf](https://acfr.aut.ac.nz/__data/assets/pdf_file/0005/265379/commodity-index-funds_Loic-Marechal.pdf).
- **Bessembinder, Carrion, Tuttle & Venkataraman (2016, JFE)**, USO roll, [link](https://www.sciencedirect.com/science/article/abs/pii/S0304405X16300113). [ABS]
  - On USO roll dates: *narrower* spreads, more depth, better resiliency. No systematic predatory trading.
  - So a predictable roll is *not* a directional signal for the outright price.
- **Relevance to Polymarket:** the outright sign is mostly unaffected. The **Active Month switch in the label (Section 0)** is the roll effect that matters.
- Tail case: expiring-contract distortions (April 2020, −$37.63). By construction Polymarket has switched away from the expiring contract 2 sessions before its last trade.
- **Test later:** the Up rate on roll sessions conditional on the sign of M2−M1. Inputs: Yahoo contract tickers or Pyth per-contract feeds.

### 2.5 Other scheduled items and seasonality
- **Baker Hughes rig count (Fri ~13:00 ET).** No peer-reviewed daily-return study found. Only practitioner claims ("unexpected rig declines → +3-5% within two weeks", unsourced). Low prior.
- **COT release (Fri 15:30 ET).** See 4.1. The release is inside the Friday window.
- **Turn-of-month / pre-holiday.**
  - No oil-specific study of turn-of-month *returns* found. Qadan et al. (2022) report turn-of-month effects in *implied volatility* for oil and gold.
  - Month-of-year: WTI up Mar-May, down Nov-Dec over 1986-2020, but "variabilities are large compared to average returns" ([CXO Advisory](https://www.cxoadvisory.com/calendar-effects/any-seasonality-for-oil-prices/)).
  - Red flag: a 30-40-year sample with one observation per year per month means very low power.

---

## 3. Price-only rules

### 3.1 Daily reversal (negative first-order autocorrelation)
- **Da, Tang, Tao & Yang (2024, Mgmt Sci 70(4))**, "Financialization and Commodity Markets Serial Dependence", [link](https://pubsonline.informs.org/doi/10.1287/mnsc.2023.4797). [FT p.1-2]
  - Causal evidence that index trading produces daily overshoot-and-reverse in *indexed* commodities (WTI is the largest GSCI weight).
  - Rolling 10-year daily AR(1) of GSCI/BCOM fell from about +0.04 before 2000 to **about −0.03 to −0.05 after 2006** (sample to 2018). Non-indexed commodities stayed at about +0.06-0.08.
  - The authors say real-time strategies profit after costs.
  - Held up? **A third-party replication covering 2006-2026 shows about −1.1%/yr, Sharpe −0.02** ([paperswithbacktest](https://paperswithbacktest.com/strategies/financialization-and-commodity-markets-serial-dependence)). [3P] So it looks weak or dead at the portfolio level recently. WTI's own AR(1) still needs checking.
  - Inputs: the prior day's close-to-close return.
  - Implied hit rate from AR(1) = −0.04 is only about 51-52% (sign dependence is roughly (2/π)·asin(ρ)).
- **Wang & Yu (2004)**, "Trading activity and price reversals in futures markets", via [Quantpedia](https://quantpedia.com/strategies/short-term-reversal-with-futures). [3P]
  - Weekly cross-sectional reversal in high-volume, low-open-interest contracts. 1983-2000, Sharpe 0.82, max drawdown −59%. Pre-2001; cross-sectional.
- **Zhang & Urquhart (2020, Rev. Behavioral Finance)**, [eprint](https://eprints.whiterose.ac.uk/id/eprint/159905/). [FT abstract]
  - 29 commodities, 1979-2017, 189 formation/holding windows. **No significant reversal profits.** Momentum is significant and grows with horizon.

### 3.2 Time-series momentum / trend
- **Huang, Li, Wang & Zhou (2020, JFE)**, "Time series momentum: Is it there?", [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3165284). [ABS]
  - Asset-by-asset regressions show **little TSM predictability in- or out-of-sample**. Pooled t-statistics fail bootstrap critical values.
  - TSM strategy profits are about the same as a historical-mean strategy.
  - A key red flag for "momentum predicts WTI direction".
- **"Is there a TSMOM effect in the Asian crude oil futures market?" (Pacific-Basin Fin. J., 2024)**, [link](https://www.sciencedirect.com/science/article/abs/pii/S0927538X24002245). [ABS] Weak or no evidence of TSMOM in Asian crude futures (recent data).
- **Marshall, Cahan & Cahan (2008, JBF)**, [SSRN](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID1028144_code114671.pdf?abstractid=1003064). [ABS]
  - More than 7,000 technical rules on 15 commodity futures (daily, 1984-2005) with White's Reality Check / Hansen SPA. **No rule beats chance after data-snooping control.** The standard warning for any technical-rule search.
- **Narayan, Ahmed & Narayan (2015, JFM)**, [link](https://onlinelibrary.wiley.com/doi/abs/10.1002/fut.21685). [ABS] Moving-average momentum profits in commodity futures are significant, but depend on data frequency and subsample.
- **Yin & Yang (2016, Energy Econ.)**, "Predicting the oil prices: Do technical indicators help?", [link](https://www.sciencedirect.com/science/article/abs/pii/S014098831630055X). [ABS]
  - 18 technical indicators beat 18 macro variables in-sample and out-of-sample (OLS plus combinations). Monthly.
- **Wen, Liu, Wang & Zhang (2022, Resources Policy 76)**, enhanced MA indicators, [IDEAS](https://ideas.repec.org/a/eee/jrpoli/v76y2022ics0301420722000216.html). [ABS]
  - Volume- and price-normalized MAs beat plain MAs for *monthly* crude futures returns. The prior 20 trading days carry the most information.
- **Lim, Zohren & Roberts (2019, JFDS)**, Deep Momentum Networks, [arXiv 1904.04912](https://arxiv.org/abs/1904.04912), and **Wood, Roberts & Zohren (2022, JFDS)**, changepoint detection, [arXiv 2105.13727](https://arxiv.org/abs/2105.13727). [ABS]
  - 88 futures with daily signals. Sharpe more than doubles versus TSMOM before costs, but the gain survives only up to 2-3bp costs.
  - The CPD version adds about 1/3 to Sharpe overall and about 2/3 in 2015-2020, *when plain TSMOM underperformed*.
  - Portfolio-level; no WTI-specific numbers.
- **Held up?** Plain TSMOM was weak in 2015-2020 and is not a reliable daily sign predictor for a single asset. Low prior for a daily Up/Down label. Worth including only as a feature.

### 3.3 Overnight / session decomposition (new facts beyond the known Wen 2021)
- **"Intraday and overnight tail risks and return predictability in the crude oil market" (Energy Econ. 127, Oct 2023)**, [IDEAS](https://ideas.repec.org/a/eee/eneeco/v127y2023ipbs0140988323006199.html). [ABS]
  - USO high-frequency data, 10 Apr 2008 – 10 May 2022.
  - First half-hour predictability comes mainly from **09:30-10:00**. **Overnight returns lose predictability.** **Extreme shocks wipe out** the first-half-hour signal. News moves overnight tail risk more than daytime.
  - This updates the known Wen 2021 result with data through 2022.
- **"Intraday return predictability in China's crude oil futures market" (Economic Modelling 96, 2021)**, [IDEAS](https://ideas.repec.org/a/eee/ecmode/v96y2021icp209-219.html). [ABS]
  - INE SC: the **night return *negatively* predicts the day return, in- and out-of-sample**. Stronger with high volume, high vol, low liquidity. Economically significant in timing tests.
  - Applies to INE, not CL. The INE night session overlaps US hours.
- **INE–WTI/Brent linkage** ([Annals of OR 2021](https://link.springer.com/article/10.1007/s10479-021-04097-x); [JES 2025](https://www.emerald.com/jes/article/52/9/215/1271430/The-impact-of-WTI-futures-on-Shanghai-crude)). [ABS]
  - Spillovers run WTI → INE, with little in reverse. INE is more integrated in its night session.
  - Implication: Asian-session INE moves are unlikely to lead CL.
- **Implication for this label:** Asia/Europe moves are *part of* the 17:00→17:00 return. They matter only through the partial-session probability (1.3) or for predicting the rest of the US session (known Wen results). No study found showing that Asia/Europe CL moves predict the *next* day's close-to-close sign.

---

## 4. Positioning and term structure

### 4.1 CFTC Commitments of Traders
- **Kang, Rouwenhorst & Tang (2020, JF 75(1))**, "A Tale of Two Premiums", [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2449315). [ABS]
  - Weekly horizon. Short-term position *changes* are driven by speculators' liquidity demand: speculators chase momentum, hedgers act as contrarians.
  - **Futures prices predictably rise (fall) the week after hedgers buy (sell).** Stronger when hedgers are funding-constrained or inventories are high.
  - Inputs: weekly commercial net position change (Tuesday positions, released Friday 15:30 ET).
  - Code: not public. Easy to rebuild with the CFTC Socrata API.
  - Held up? **Maréchal (2023, JFM)** "A tale of two premiums revisited", [IDEAS](https://ideas.repec.org/a/wly/jfutmk/v43y2023i5p580-614.html), re-tests with other methods and a pre/post financialization split (1994-2017). Recent (2018-2026) evidence not found.
  - Red flags: cross-sectional Fama-MacBeth on many commodities; weekly, not daily; the per-WTI effect size was not verified.
- **Büyükşahin & Harris (2011, Energy J.)**, "Do speculators drive crude oil futures prices?", [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1728705). [ABS]
  - Non-public daily CFTC position data, Granger tests. **Price changes lead speculator position changes, not the reverse.**
  - Red flag for "managed-money positioning predicts direction".
- **Sanders, Boris & Manfredo (2004, Energy Econ.)**, energy COT, [link](https://www.sciencedirect.com/science/article/abs/pii/S0140988304000209). [ABS] Positive returns lead to higher non-commercial net longs the following week. Little evidence that positions predict returns.
- **Yu, Chen, Wang & Zhang (2023, Economic Modelling 121)**, Hedging Pressure Momentum index, [IDEAS](https://ideas.repec.org:443/a/eee/ecmode/v121y2023ics0264999323000263.html). [ABS]
  - Long- and short-horizon hedging pressure combined via momentum rules plus scaled PCA. 1994-Jun 2021. **R²_OS = 0.946%**, beating popular predictors. Works partly through a sentiment channel.
  - Frequency is probably monthly (not verified).
  - Red flag: an engineered index (PCA over rule variants) invites specification search.

### 4.2 Term structure / basis
- **Bredin, O'Sullivan & Spencer (2021, Energy Econ. 100)**, "Forecasting WTI crude oil futures returns: Does the term structure help?", [IDEAS](https://ideas.repec.org/a/eee/eneeco/v100y2021ics0140988321002565.html). [ABS]
  - Dynamic Nelson-Siegel level/slope/curvature factors of the CL curve predict holding-period returns in-sample.
  - Out of sample, NS plus macro (LASSO) **significantly beats no-change**. Trading Sharpe beats buy-and-hold and historical mean. Directional accuracy improves.
  - Horizons span multiple holding periods, with the best results at medium horizons. The daily horizon is not confirmed.
  - Inputs: daily settlements of the CL 1..N contracts.
- **Classic basis/carry evidence** (Gorton-Hayashi-Rouwenhorst; Szymanowska et al.) is cross-sectional and monthly. Stylized fact: crude bear markets mostly happen in contango.
- **Daily data warning:** EIA's free NYMEX CL contract 1-4 series (RCLC1-4) **stopped on 2024-04-05** (verified on the page). Use Yahoo contract tickers (e.g. `CLZ26.NYM`, responded) or Pyth per-contract feeds instead.

---

## 5. Volatility, uncertainty and tail predictors

- **OVX / variance risk premium.**
  - **Kang & Pan (2015)**, "Commodity Variance Risk Premia and Expected Futures Returns: Evidence from the Crude Oil Market", [summary](https://paperswithbacktest.com/strategies/commodity-variance-risk-premia-and-expected-futures-returns-evidence-from-the-crude-oil-market). [3P]
    - VRP (expected realized variance − implied variance) **negatively predicts** crude futures returns after controls.
    - Horizon weekly-to-monthly (rebalancing monthly in the summary).
    - Needs options or OVX plus high-frequency realized variance.
  - **Le Grand & Schneider (2022)** [pdf](https://francois-le-grand.com/docs/research/LS_vrp.pdf) [FT p.1-3]: a two-state regime-switching stochastic volatility model on OVX, May 2007 – Mar 2020. A normal state (negative VRP) and a crisis state (positive VRP). A regime model, not a return forecast. They exclude April 2020 as a singular event.
  - No study found showing that the OVX *level* predicts next-day WTI *sign*. OVX is best used as a conditioning variable: reversal and momentum strength, and σ for 1.3.
- **GARCH-in-mean.**
  - **"Investigating the risk-return trade-off for crude oil futures using high-frequency data" (Applied Energy 2017)**, [link](https://www.sciencedirect.com/science/article/abs/pii/S0306261916317433). [ABS] **No risk-return trade-off**; a weak negative link between downside risk and expected return.
  - Combined with 1.2, GARCH-M is a low prior for direction.
  - Code: `arch` has `ARCHInMean` ([docs](https://arch.readthedocs.io/en/latest/univariate/mean.html)); R `rugarch` (archm=TRUE).
- **Markov switching.**
  - MS-GARCH studies on oil ([arXiv 1512.01676](https://arxiv.org/pdf/1512.01676); [Herrera et al.](https://gattonweb.uky.edu/faculty/herrera/documents/HHP.pdf)) report "success rates >68% at 1-5 days". That is **volatility** direction, not price. Red flag if quoted as return accuracy.
  - **Semiparametric MS for OPEC/WTI/Brent prices (Energy Econ. 2018)**, [link](https://www.sciencedirect.com/science/article/abs/pii/S014098831830238X). [ABS] Beats ARIMA/GARCH on price *levels* (RMSE). No hit rate.
  - Code: statsmodels `MarkovRegression`/`MarkovAutoregression`; R `MSwM`, `MSGARCH`.
- **Geopolitical risk (GPR).** Salisu/Gupta papers: GPR predicts oil *volatility* and *tail risk* out-of-sample, mostly monthly.
  - **"Geopolitical risk trends and crude oil price predictability" (Energy 2022)**, [link](https://www.sciencedirect.com/science/article/abs/pii/S0360544222017273). [ABS] GPR *trend* variables beat the historical average out-of-sample, with mean-variance utility gains. Monthly.
  - A daily GPR index exists (see data table). Daily-horizon return evidence is thin.
- **EPU.** Bekiros et al. (2015) find EPU helps predict oil returns out-of-sample (monthly). Causality-in-quantiles work finds it matters mainly in distress and at extreme quantiles. [ABS, via search summaries] Daily EPU (FRED `USEPUINDXD`) is available. Daily-horizon evidence is weak.
- **Quantile regression / tail risk.**
  - **Zhang & Zhao (2025, JFM 45(7))**, "Tail Risks Everywhere and Crude Oil Returns", [link](https://onlinelibrary.wiley.com/doi/abs/10.1002/fut.22586). [ABS]
    - High-dimensional tail risks from oil plus the US financial market, in a predictive LASSO quantile model. **Beats RW out-of-sample.**
    - Higher tail risk means lower returns in bear quantiles and higher in bull quantiles. That asymmetry matters for sign.
    - Frequency not verified.
  - "OPEC news and predictability of energy futures returns and volatility: conditional quantile regression" ([SciELO 2020](http://www.scielo.org.pe/scielo.php?script=sci_arttext&pid=S2077-18862020000200239)). [ABS]
  - Code: statsmodels `QuantReg`; R `quantreg`.

---

## 6. Model families: expected daily performance and red flags

| Family | Best oil evidence | Horizon | Recent hold-up | Red flags | Code |
|---|---|---|---|---|---|
| ARIMA / ARIMAX | Drachal 2016 found DMA *not* better than ARIMA; ARIMA is itself about RW for daily prices | daily-monthly | RW dominance persists (Ellwanger & Snudden 2023) | Many "beats ARIMA" papers score on price *levels* (RMSE), not sign | statsmodels `SARIMAX`; R `forecast` |
| VAR / VECM with Brent and products | WTI leads Brent slightly on daily data, 1987-2017 (TOPS method), but the link is unstable around extreme events ([Frontiers Phys. 2020](https://public-pages-files-2025.frontiersin.org/journals/physics/articles/10.3389/fphy.2020.00132/text)). Spread determinants: [JFM 2021](https://onlinelibrary.wiley.com/doi/full/10.1002/fut.22184). **Crude futures predict *products*** (Energy 2024, [link](https://www.sciencedirect.com/science/article/abs/pii/S0360544224025246)), not the reverse. Baumeister-Kilian-Zhou 2018 ([link](https://www.cambridge.org/core/journals/macroeconomic-dynamics/article/abs/are-product-spreads-useful-for-forecasting-oil-prices-an-empirical-evaluation-of-the-verleger-hypothesis/39D3BB6195C1118F8221A82C3FB0777E)): product spreads help monthly, at some horizons | daily lead-lag; monthly spreads | unstable | Brent (ICE) settles 19:30 London; timing mismatch inflates "lead" | statsmodels `VAR`, `VECM`; R `vars`, `urca` |
| Logit / probit direction | Few clean oil papers. ML-vs-logit comparisons on metals claim 85-90% at 20 days vs logit 55-60% | daily-20d | – | **Overlapping multi-day windows and leakage make 85-90% implausible.** Use non-overlapping daily labels and Pesaran-Timmermann tests | statsmodels `Logit`/`Probit`; R `rugarch::DACTest` |
| Quantile regression | Zhang & Zhao 2025 (above) | daily/monthly | 2025 publication | high-dimensional selection | `QuantReg`, `quantreg` |
| Markov switching | MS for levels/vol (above) | daily | – | vol-direction accuracy misreported as price direction | `MarkovRegression`, `MSwM`, `MSGARCH` |
| Kalman / TVP | Bredin et al. 2021 (TVP NS decay). Kalman-weighted combinations on monthly prices ([Energy 2018](https://www.sciencedirect.com/science/article/abs/pii/S0360544218300070)). TVP forecast combinations ([Energy Econ. 2017](https://www.sciencedirect.com/science/article/abs/pii/S014098831730244X)) | monthly | – | smoothed (not filtered) states leak future data. Always use filtered states | statsmodels `UnobservedComponents`/`MLEModel` |
| Bayesian DMA / BMA / mixtures | Drachal 2016 ([RG](https://www.researchgate.net/publication/308877758)), OOS May 2003-Dec 2011: no better than ARIMA. Naser 2016 DMA ([link](https://www.sciencedirect.com/science/article/abs/pii/S0140988316300329)). Drachal & Pawłowski 2021, dynamic finite mixtures ([link](https://www.sciencedirect.com/science/article/abs/pii/S0140988321001882)) | monthly | – | monthly macro predictors | R `fDMA` (by Drachal), `eDMA`; PyMC |
| Forecast combination | Baumeister-Kilian 2015; Garratt et al. 2019 | 1-24m | real-time vintages | not daily | trivial to build |
| HAR (returns) | HAR is for volatility. As a σ input it feeds 1.3 | daily | holds (known) | – | `arch` HARX |
| GARCH-M | no trade-off (2017) | daily | – | see 1.2 | `arch` `ARCHInMean`, `rugarch` |

**Data-snooping guards to use later:** Clark-West (nested R²_OS), Diebold-Mariano, Pesaran-Timmermann (sign), White Reality Check / Hansen SPA / Romano-Wolf StepM / Model Confidence Set. All are in `arch.bootstrap` (`SPA`, `StepM`, `MCS`). Always split out 2020-2026 as a hold-out.

---

## 7. Reinforcement learning on oil

| Paper | Target / data | Reported OOS | Honest? |
|---|---|---|---|
| Zhang, Zohren & Roberts (2020, JFDS), "Deep RL for Trading", [arXiv 1911.10107](https://arxiv.org/abs/1911.10107) | 50 liquid futures (commodities incl. energy), daily; test 2011-2019 | Beats TSMOM/MACD baselines, "positive profits despite heavy transaction costs" | The most credible: multi-asset, long test, costs included. No WTI-only numbers in the abstract |
| TBDQN two-branch DQN (Applied Energy 2023), [link](https://www.sciencedirect.com/science/article/abs/pii/S0306261923006852) | crude and natural gas futures, technical indicators + OHLCV | "excellent" annual return / Sharpe | Single split, heavy architecture search. **High overfit risk** |
| Ensemble DRL with volume-price time-frequency decomposition (Energy 2023), [ADS](https://ui.adsabs.harvard.edu/abs/2023Ene...28529394D/abstract) | WTI futures | selects agents by Sharpe | Decomposition (EMD/VMD-type) on the full series is a common **look-ahead leak** |
| "Crude oil price prediction using deep RL" (Resources Policy 2023), [link](https://www.sciencedirect.com/science/article/abs/pii/S0301420723000715) | price prediction | – | abstract-level only |
| Hanetho (2023), [arXiv 2308.01910](https://arxiv.org/abs/2308.01910) | **natural gas** front-month, 2017-2022 | Sharpe 83% higher than buy-and-hold | Not oil. Buy-and-hold is a weak baseline |

Bottom line: no WTI-specific RL paper found with a long, costed, walk-forward test that would justify a daily-sign prior.

---

## 8. Recurring red flags in this literature
1. **Monthly-average prices** used as the target or benchmark (Ellwanger & Snudden 2023).
2. **Volatility-direction accuracy** (MS-GARCH, HAR) reported as if it were price direction.
3. **Decomposition leakage** (EMD/VMD/wavelet on the full sample) and **smoothed Kalman states**.
4. **Overlapping multi-day labels** inflating accuracy (20-day 85-90% claims).
5. **Rule searches without SPA/Reality Check** (Marshall et al. 2008 wiped out 7,000 rules).
6. **Pre-2010 microstructure effects** (Goldman roll, pre-announcement drift) weakened by later arbitrage or release-policy changes.
7. **Label mismatch:** spot (DCOILWTICO) or 14:30 settlement versus Pyth's 17:00 Active Month close; roll-day contract switching.
8. **April 2020** negative prices: treat as a special case or winsorize; many papers stop in Feb/Mar 2020.

---

## 9. Free data sources (checked 2026-09-14)

| Input | Source | Status verified | Notes |
|---|---|---|---|
| Resolution-grade CL price (per contract, 1-min) | Pyth Benchmarks `benchmarks.pyth.network/v1/price_feeds/?query=WTI&asset_type=commodities` | **responded**; feeds such as `WTIV6`, `WTIX6`; schedule 00-17 & 18-24 ET | exact label source; history depth not checked |
| CL daily (continuous and per contract) | Yahoo chart API `query1.finance.yahoo.com/v8/finance/chart/CL=F` and `CLZ26.NYM` | **responded** (CL=F 103.24 live) | unofficial; the daily close may be settlement, not 17:00 |
| NYMEX CL contract 1-4 (EIA RCLC1-4) | eia.gov/dnav/pet/pet_pri_fut_s1_d.htm | **page up, but data ends 2024-04-05** | don't use for 2024+ |
| WTI spot | FRED `DCOILWTICO` | **latest obs 2026-09-09** (≈3-day lag) | spot, not futures |
| Brent spot | FRED `DCOILBRENTEU` | **2026-09-09** | lagged |
| OVX | FRED `OVXCLS` (from 2007-05-10); CBOE `cdn.cboe.com/api/global/us_indices/daily_prices/OVX_History.csv` | **FRED 2026-09-10; CBOE file 2009-09-18 → 2026-09-11** | CBOE is fresher |
| Products (NYH gasoline, heating oil spot) | FRED `DGASNYH`, `DHOILNYH` | **2026-09-09** | for crack spreads |
| Daily EPU | FRED `USEPUINDXD`; policyuncertainty.com `All_Daily_Policy_Data.csv` | **FRED 2026-09-10; CSV responded** | noisy daily |
| Daily GPR | matteoiacoviello.com `gpr_files/data_gpr_daily_recent.xls` | **responded** (xls) | daily from 1985 per the author (not re-checked) |
| EIA WPSR release calendar | eia.gov/petroleum/supply/weekly/schedule.php | **verified**, holiday Thursday dates listed | Wed 10:30 ET |
| EIA inventories (weekly actuals) | EIA API v2 `api.eia.gov/v2/petroleum/...` | **403 without key** (free key needed); `ir.eia.gov/wpsr/overview.pdf` responded | consensus forecasts are not free |
| API weekly bulletin | api.org WSB | paid | headlines in news, Tue 16:30 ET |
| CFTC COT (disaggregated, futures only) | Socrata `publicreporting.cftc.gov/resource/72hh-3qpy.json`, code `067651` (WTI-PHYSICAL NYMEX) | **verified: 2006-06-13 → 2026-09-08**; latest managed money L/S 218,960 / 107,229 | released Fri 15:30 ET |
| CFTC COT (legacy, futures only) | Socrata `6dca-aqww`, code `067651` | **verified: 1986-01-15 → 2026-09-08** | commercial / non-commercial split |
| OPEC announcement dates and surprises | github.com/dkaenzig/oilsupplynews | **responded** | daily surprise series (sample end not checked) |
| Baker Hughes rig count | rigcount.bakerhughes.com | **403 to scripted curl** (bot wall) | likely fine in a browser; Fri ~13:00 ET |
| NY Fed Oil Price Dynamics (supply/demand decomposition) | newyorkfed.org | **discontinued Nov 2023**; archive data downloadable | history only |

---

## 10. Test-worthy shortlist (ranked by prior × test cost; recent evidence emphasized)

1. **Roll-session label mechanics** (Section 0). Check whether the switch day compares across contracts. If it does, the sign of the M2−M1 spread gives a structural tilt about 12 times a year. Cost: a rules check plus a query of Pyth per-contract history.
2. **Partial-session probability Φ(r/σ_rem)** versus Polymarket odds (1.3). This is the benchmark for any intraday entry. Use HAR/OVX for σ.
3. **EIA-release-day premium.** WTI Wednesday premium 2007-2021 (Li et al. 2022). Natural gas analogue: more than 50% of annual return on EIA days (Prokopczuk 2021). INE EIA × Thursday effect (2025). Test with the real release day, 2007-2026, with a 2021-2026 hold-out.
4. **API Tuesday → EIA Wednesday chain** and next-week partial reversal of inventory surprises (Ederington et al. 2019). Needs API headlines (news) and the EIA actual (free).
5. **Daily reversal AR(1)** on the WTI label (Da et al. 2024: index AR(1) ≈ −0.04 after 2006). Cheap. The 2006-2026 third-party replication is about zero, so the prior is low, but it is a necessary baseline.
6. **Weekly COT hedger-flow tilt** (Kang-Rouwenhorst-Tang 2020) using free CFTC 1986-2026 data. Carry the weekly tilt into Mon-Fri labels. Also test the reverse causality (Büyükşahin & Harris 2011).
7. **OPEC/OPEC+ decision-day handling.** Cuts are positive but fade within a week after 2015 (Verdad 2022). "Maintain" is negative. Känzig dates. Use as a regime flag plus a day+1..+5 fade.
8. **Term-structure slope / Nelson-Siegel factors** (Bredin et al. 2021, out-of-sample beats no-change with trading value). Rebuild the curve from Yahoo/Pyth contracts; EIA series are dead after 2024.
9. **OVX / VRP conditioning.** VRP negatively predicts returns (Kang & Pan 2015). Use OVX regime to switch between reversal and momentum and to scale σ. 2023 update: extreme shocks kill the intraday signals.
10. **DOW / weekend dummies and seasonality**, as a cheap sanity layer only. Monday effects are time-varying (Li et al. 2022; Yue et al. 2025), and survey-style summaries say calendar anomalies have faded in recent years.

Low priors, not worth building first: GARCH-M, MS-GARCH for price sign, plain TSMOM for a single-asset daily sign, DMA/BMA with monthly macro data, RL agents, 20-day "85-90%" ML direction claims.
