# RailETA — Dynamic Train Arrival Prediction

RailETA is a minimum working prototype for an Indian coaching-train ETA dashboard. It replays simulated GPS journeys for two illustrative Rajdhani routes, snaps positions to a route, detects stop events, and predicts the remaining journey section by section. Each station shows the timetable, the mandated **scheduled arrival + current delay** baseline, and an explainable prediction with uncertainty. Flask is the only direct Python dependency; the frontend is plain HTML/CSS/JavaScript with Leaflet 1.9.4 and OpenStreetMap. It runs locally and is packaged for Vercel.

**All data is simulated.** Train names carry `(DEMO)`, station coordinates/distances are approximate, and timetable times are illustrative rather than official. RailETA has no connection or affiliation with Indian Railways, CRIS, RTIS, or NTES. No official service is scraped or contacted. The feed format is designed so an authorised feed or consenting onboard device could replace the simulator later.

## Run on a laptop

Use Python 3.10 or newer. In a terminal, enter this project's `raileta` directory, then:

```sh
pip install flask
python generate_data.py
python evaluate.py
python app.py
```

Open **http://localhost:5000**. The dashboard automatically tracks 12301; enter **12302** for the reverse journey. Leave the terminal running. Press Ctrl+C there to stop the server.

On systems where the executable is `python3`, use `python3 -m pip install flask` and replace `python` with `python3` in the commands. On Windows, `py -m pip install flask` and `py <script>.py` are equivalent. If the OS protects its Python installation, use a virtual environment:

```sh
python -m venv .venv
```

Activate it with `source .venv/bin/activate` on macOS/Linux, `.venv\Scripts\activate.bat` in Windows Command Prompt, or `.venv\Scripts\Activate.ps1` in PowerShell. Then use the four commands above. No npm or frontend build is needed. An internet connection is needed only for the Leaflet CDN and map tiles; the local API and predictions use bundled files.

Tests use the standard library and Flask's own test client:

```sh
python -m unittest -v test_raileta
```

The code and path handling are portable; checks were executed on Linux, not on physical Windows/macOS machines. The requested `pip install flask` installs Flask's ordinary transitive dependencies; there are no other direct pip dependencies.

## Deploy on Vercel

The deployable project root is **`raileta/`**, not its parent directory.

1. Commit/upload this project to your own Git repository, including the generated files in `data/`.
2. Import that repository in Vercel and set **Root Directory** to `raileta` if the repository contains the outer folder. If this folder itself is the repository root, use `.`.
3. Use the **Flask** framework preset (or let Vercel detect it). Keep the default install command. The checked-in configuration sets the build command to `python build.py`; leave the output directory at the framework default.
4. Deploy. `app.py` exports the Flask instance; `requirements.txt` lists the sole direct dependency. `build.py` stages the three frontend source files into `public/`, which Vercel serves through its CDN. API routes run as a Python function, and the small JSON/CSV dataset is included in that function.
5. Optionally set a stable `RAILETA_SESSION_SECRET` environment variable for signing demo preferences, then redeploy. The built-in fallback is public and deliberately protects no identity, credentials, permissions, or private data. There is no authentication in this prototype.

No server writes a database or modifies deployed files. The replay clock and up to eight manual reports live in a small signed browser-session cookie. Each request reconstructs the same clock on any serverless worker; visitors have independent replay controls. Read-only requests avoid rewriting the cookie, so a concurrent poll cannot overwrite a newer control action. Python `ContextVar` keeps request state separate inside a shared worker. Immutable route/history caches may live in memory safely. A speed change preserves the current simulated instant; restart clears manual reports and anchors the journey to today's IST date.

For a build packaging check without Vercel:

```sh
python build.py
```

`public/` is generated, ignored by Git, and should not be edited. Local Flask continues serving `static/` directly. Runtime needs no generation command, API keys, persistent filesystem, scheduled jobs, or extra packages. Vercel may introduce a cold start; the local under-two-second behavior is not a guarantee for every hosted cold start or network.

Configuration follows [Vercel's Flask documentation](https://vercel.com/docs/frameworks/backend/flask). Local packaging and serverless-session behavior have been tested; a deployment to an actual Vercel account has **not** been performed.

## Architecture

```text
Recorded replay feed ----+
                         |
Future authorised feed --+--> /api --> state.py
or onboard phone         |            | snap GPS / reject outliers
(not implemented) -------+            | detect stop events / current delay
                                      v
                           predictor.py <--- history statistics
                                      ^ <--- reported conditions
                                      |
                                      v
                             Flask JSON APIs
                              /      |      \
                      Dashboard   Apps   Station displays
```

The optional future sources above describe an interface, not an existing integration.

## How the prediction works

1. **Locate the train.** Project GPS onto the complete STOP+PASS polyline and interpolate the supplied rail kilometre values. Reject points more than 2 km away or more than 0.3 km behind established progress. Arrival and departure require observed pings crossing the station's ±0.5 km thresholds.
2. **Learn each section.** Historical actual departure-to-arrival durations become per-section samples. Speed-restriction penalties are removed algebraically. Prefer samples with the same congestion, fog, and departure-delay bucket; if fewer than five exist, progressively relax the bucket, fog, and congestion filters. Use the median and P10/P90 of the chosen samples.
3. **Predict what remains.** From the current position, scale the section runtime by the fraction of distance left. Within 30 km, blend with the latest speed if it exceeds 20 km/h. Then add expected station dwells and each following section. Departure is always at least the scheduled departure. Add explicit speed-restriction penalties and condition adjustments when matching samples are unavailable.
4. **Explain the result.** Accumulate section/dwell uncertainty in quadrature; widen it 25% with estimated GPS. Confidence is HIGH for total width ≤10 minutes, MEDIUM for ≤25, otherwise LOW. Add condition chips and exactly one recovery/losing-time/in-line chip. The baseline remains scheduled arrival plus the most recent station-based delay.

During a GPS gap, after 120 simulated seconds the position is **ESTIMATED** using typical historical section speed. It cannot cross the next station without confirmation. After 600 seconds it carries **SIGNAL_LOST**. The simulator's 8-minute gap triggers ESTIMATED but is intentionally too short to trigger SIGNAL_LOST; the longer-loss case is tested separately. At a mid-section signal halt, the location stays still while the clock advances, naturally moving the ETA later.

The same `predict_journey(train, state, sim_now, active_conditions, stats)` function serves the live API and backtest. The live service builds statistics from all 60 available historical days. The backtest strictly uses the oldest 45 days. It never uses held-out arrivals, departures, or hidden noise to train statistics.

## Evaluation

The generator creates 60 historical journeys per train with `random.seed(42)`. Training uses the first 45 dates and testing uses the last 15, giving 30 held-out journeys and 840 origin/destination predictions. At every just-departed stop, both models forecast all later stops.

**Assumption:** condition flags for future sections are known before the train reaches them. Only flags and restriction parameters are supplied; hidden run-time noise, random congestion duration, random fog multiplier, and future actual outcomes are not. This is a favourable information assumption for the model, not a real-world performance claim.

| Group | Baseline MAE | Model MAE | Improvement | Within ±10 min (baseline / model) |
|---|---:|---:|---:|---:|
| 1 stop ahead | 9.99 min | 5.01 min | 49.9% | 60.0% / 87.6% |
| 2–3 stops ahead | 14.90 min | 7.34 min | 50.7% | 49.1% / 75.5% |
| 4+ stops ahead | 23.39 min | 10.82 min | 53.7% | 36.0% / 62.0% |
| <150 km | 3.34 min | 2.98 min | 10.9% | 96.7% / 100.0% |
| 150–500 km | 11.97 min | 5.92 min | 50.5% | 54.6% / 83.8% |
| >500 km | 22.06 min | 10.29 min | 53.4% | 36.7% / 62.4% |
| Overall | 16.71 min | 8.00 min | 52.1% | 47.1% / 73.7% |

MAE and median absolute error measure typical error; bias is mean signed error (negative means predictions are early). Full MAE, median, bias, ±5-minute and ±10-minute metrics by horizon/distance are in `data/evaluation.json` and printed by `python evaluate.py`. The dashboard reads that JSON directly; it does not hardcode the results.

The generator was not tuned after evaluation. All reported horizon groups improve in this generated dataset, while the <150 km distance group improves only modestly. Individual predictions can be worse than the baseline, especially around abrupt condition changes, sparse historical subgroups, and unforeseen halts. The scripted current replay is a separate illustration and does not guarantee improvement at every moment. Intervals are heuristic spreads, not calibrated coverage guarantees.

## API reference

All API responses and route errors are JSON. Times use ISO-8601 with `+05:30`. UI times are HH:MM with `+1` for the next calendar day. `/` and `/static/*` naturally return page/assets, not JSON.

The session is stored in a cookie. Reuse a cookie jar in curl when changing replay controls; otherwise every independent client starts at minute 20. These examples are for a POSIX shell (on Windows use `curl.exe`, with shell-appropriate JSON quoting).

| Method | Endpoint | Returns |
|---|---|---|
| GET | `/` | Dashboard HTML |
| GET | `/api/trains` | Number, name, origin, destination for every train |
| GET | `/api/train/<no>/live` | Position, status, every stop, predictions, route, active conditions |
| GET | `/api/train/<no>/eta` | Compact upcoming arrivals for passenger applications |
| GET | `/api/station/<code>/arrivals` | Upcoming arrivals across demo trains, sorted by predicted ETA |
| GET / POST | `/api/sim` | Read/control speed, restart, station jump, demo-event jump |
| GET / POST / DELETE | `/api/conditions` | Read current conditions, add a manual report, clear personal reports |
| GET | `/api/evaluation` | Chronological held-out backtest results |

```sh
curl http://localhost:5000/
curl -c cookies.txt -b cookies.txt http://localhost:5000/api/trains
curl -c cookies.txt -b cookies.txt http://localhost:5000/api/train/12301/live
curl -c cookies.txt -b cookies.txt http://localhost:5000/api/train/12301/eta
curl -c cookies.txt -b cookies.txt http://localhost:5000/api/station/CNB/arrivals
curl -c cookies.txt -b cookies.txt 'http://localhost:5000/api/sim?train_number=12301'
curl -c cookies.txt -b cookies.txt -X POST http://localhost:5000/api/sim -H 'Content-Type: application/json' -d '{"train_number":"12301","speed":120}'
curl -c cookies.txt -b cookies.txt -X POST http://localhost:5000/api/sim -H 'Content-Type: application/json' -d '{"train_number":"12301","jump_before":"GAYA"}'
curl -c cookies.txt -b cookies.txt -X POST http://localhost:5000/api/sim -H 'Content-Type: application/json' -d '{"restart":true}'
curl -c cookies.txt -b cookies.txt 'http://localhost:5000/api/conditions?train_number=12301'
curl -c cookies.txt -b cookies.txt -X POST http://localhost:5000/api/conditions -H 'Content-Type: application/json' -d '{"train_number":"12301","type":"CONGESTION","extra_min":15}'
curl -c cookies.txt -b cookies.txt -X POST http://localhost:5000/api/conditions -H 'Content-Type: application/json' -d '{"train_number":"12301","type":"SPEED_RESTRICTION","restricted_km":25,"speed_kmh":30}'
curl -c cookies.txt -b cookies.txt -X POST http://localhost:5000/api/conditions -H 'Content-Type: application/json' -d '{"train_number":"12301","type":"WEATHER_FOG","factor":1.15}'
curl -c cookies.txt -b cookies.txt -X DELETE http://localhost:5000/api/conditions
curl http://localhost:5000/api/evaluation
```

`from`/`to` can optionally specify STOP codes in travel order. Omitted endpoints select the current/next section for congestion/restriction and the entire remaining route for fog. Reports with identical train/type/endpoints replace the earlier report. Clear removes only manual reports; scripted conditions follow their original times. Report parameters are bounded and validated. Conditions on a span apply to each contained STOP-to-STOP section; `restricted_km` is capped by each section length.

`/api/sim` accepts speeds `1`, `10`, `60`, `120`. Jumping lands 10 minutes before the station's first recorded arrival threshold. Two convenience event jumps make a short jury presentation reliable:

```sh
curl -c cookies.txt -b cookies.txt -X POST http://localhost:5000/api/sim -H 'Content-Type: application/json' -d '{"train_number":"12301","jump_event":"congestion","speed":120}'
curl -c cookies.txt -b cookies.txt -X POST http://localhost:5000/api/sim -H 'Content-Type: application/json' -d '{"train_number":"12301","jump_event":"gps_gap","speed":1}'
```

They read event timing only for replay navigation; the predictor never sees future replay pings. Unsupported events on other trains return a friendly JSON error.

An unknown train returns status 404 with `{"error":"Unknown train number","available":["12301","12302"]}`. Malformed controls return status 400, and unsupported HTTP methods return JSON 405. The server logs internal errors locally but never exposes stack traces to API clients. A failed browser poll keeps the last good view and retries after five seconds.

## Five-minute jury demo

1. **30 s — State the problem.** “Timetable + current delay is how ETAs are often estimated today. We predict the remaining journey instead.” Clarify the simulated-data badge immediately.
2. **45 s — Track 12301.** Walk through the map, train status, and the timeline's three columns: Scheduled, Baseline, Predicted. Scroll the right panel to see the remaining stations.
3. **60 s — Set 60×.** The train moves and the display refreshes every five real seconds. Point out the recovery chip on an upcoming stop and the speed-restriction overlay. The “Updated” label counts real seconds since the latest successful API response; GPS timestamps use simulated IST.
4. **60 s — Demonstrate congestion.** Set **120×**, choose **Congestion report** under Demo events in the jump selector, and click Go. This lands at offset 328; C2 starts at 330 (about one real second later) and clears at 420 (about 46 seconds after the jump). The NDLS ETA rises and falls, and both changes appear in the log. Choose **1×** afterward if you want time to explain. Manual what-if buttons offer an immediate alternative.
5. **30 s — Show the GPS gap.** Set **1×**, choose **GPS gap**, and click Go. The train switches to ESTIMATED with a faded/dashed marker, and the API still shows the age of its last GPS fix. This shortcut prevents skipping the short gap at high replay speed. Later choose 60× to see measured updates return.
6. **45 s — Open Model accuracy.** Compare baseline and model MAE by horizon. Explain that these simulated results improve more at distant stations (49.9%, 50.7%, 53.7%), and show the chronological split and information assumption below the table.
7. **30 s — Show `/api/train/12301/eta`.** Open it in the same browser to share the replay cookie: “A passenger app or station display can consume this.” Finish with the architecture diagram and replacing the simulator with an authorised feed.

## File guide and recorded design choices

| File | Responsibility |
|---|---|
| `generate_data.py` | Seed route/conditions; 60 histories per train; smooth replay ramps, restrictions, signal halt, GPS gap and outliers |
| `geo.py` | Haversine distance, local equirectangular projection, rail-km interpolation |
| `replay.py` | Per-session lazy clock, recorded ping schema, speed/restart/jump handling |
| `state.py` | Reject outliers/backwards points, confirm station events, compute delay, estimate stale positions |
| `predictor.py` | Historical section/dwell statistics and the shared remaining-journey predictor |
| `app.py` | Flask routes, payload assembly, validation, session persistence and JSON errors |
| `evaluate.py` | Chronological split, shared-model backtest, console and JSON results |
| `static/index.html` | Accessible semantic dashboard structure and modal |
| `static/style.css` | Responsive layout, timeline, markers, state badges and transitions |
| `static/app.js` | Map layers, polling, render functions, control requests and change tracking |
| `test_raileta.py` | 15 standard-library acceptance tests covering the core mechanics and API |
| `build.py`, `vercel.json`, `requirements.txt` | Vercel asset staging, function configuration and sole direct dependency |
| `data/*` | Small generated local dataset; no database server |

- Default replay is offset 20 minutes at 60×. Both train routes use the same elapsed-offset clock within a browser; switching trains preserves that replay point. Different browsers have independent sessions.
- Journey date is today's IST date when the replay session begins; it stays fixed through simulated midnight. Restart anchors it to the current real IST day. The date picker is limited to the current replay date; arbitrary-date journeys are outside this prototype.
- The forward replay arrives about 20 minutes late; the reverse replay is simpler and arrives about 5 minutes early. No intermediate departure is early. GPS threshold detection can timestamp an event slightly differently from the scripted exact halt/departure time.
- Speed ramps cover approximately the first/last 3 km of each section. Sasaram restriction pings follow 30 km/h. The forward replay has an eight-minute missing-ping window between PRYJ and CNB and three off-route points near offset 250 minutes.
- All departures respect the timetable; arrivals may be early. The origin has no arrival timestamp. At the destination, predictions stop and the hero shows the GPS-confirmed actual arrival.
- Fog is applied to unrestricted running before adding the explicitly speed-capped restriction penalty. SR normalization inverts that physical equation, rather than trying to estimate normal speed from the restricted observed runtime.
- Quantiles use linear interpolation. Duplicate same-type manual reports replace earlier ones. A restriction that would be faster than normal operation contributes no negative penalty.
- PASS points help draw/snap the route and have observed passing times; optional passing ETA predictions are not implemented.
- New trains can be added by editing `stations.json` and `trains.json` only and restarting. Require increasing rail km, valid station codes, and ordered STOP schedules. Without a `replay_<number>.csv`, a neutral on-time replay is synthesized from the timetable; without history, the predictor uses scheduled run/halt times with a zero learned spread. Those fallback confidence estimates are not evidence of accuracy. Add history and recorded pings for meaningful predictions. No train-specific runtime branches exist.
- `generate_data.py` regenerates the provided seed JSON as well as CSVs. It will overwrite custom route edits; preserve them separately or edit the generator's seed definitions if regeneration is needed. Generated data remains checked in for deployment.
- Reproducibility is relative to a date: `python generate_data.py --date 2026-09-23` gives byte-identical output for the same seed/date. By default, the 60 historical dates end yesterday. The deployed dataset is a fixed synthetic sample, while replay timestamps are anchored at runtime.
- Static leaflets/map tiles can fail offline; the page displays a map notice while keeping train data. GPS coordinates are synthesized along a polyline, not verified track geometry.

## Limits and next steps

- Replace synthetic pings with an **authorised RTIS/NTES-style feed** or a consenting onboard phone; this requires the appropriate permission and integration work.
- Train and evaluate on **real historical data**, calibrate interval coverage, and test geographic and seasonal generalisation. The current data-generating process is deliberately simple and known to the team.
- Compare the conditional-median baseline to a **gradient-boosting model** for section running time once genuine training data is available.
- Support thousands of trains with independent per-train prediction workers, a proper database, and push updates instead of browser polling. This demo scans its small replay files on each request; a production feed should incrementally update state.
- The optional onboard GPS tracker, station-board page, ETA-history chart and 1,000-train benchmark are **not implemented**; they require the requested separate stretch-feature approval.

**Data disclaimer:** This is an unaffiliated demonstration using simulated trains, approximate coordinates and distances, and illustrative timetables. It is not a passenger travel advisory or an official railway information service.
