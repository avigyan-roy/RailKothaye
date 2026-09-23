# RailETA — Dynamic Train Arrival Prediction

RailETA is a Flask and vanilla JavaScript train dashboard that displays only RailRadar's live train status, current timetable and arrival projections. Demo mode, replay controls and simulation API routes have been removed. The existing layout, Leaflet map and homepage routes are retained; Flask remains the only direct Python dependency.

## Enable live data on Vercel

The Git repository is the `RailKothaye` folder. The API key stays in the Python server's environment; never put it in JavaScript, `vercel.json`, a Git commit or a screenshot.

1. In GitHub Desktop, select **RailKothaye**. Review the changed files, enter the commit summary `Remove demo and keep live train data`, click **Commit to main**, then **Push origin**. Alternatively, from this repository:
   ```sh
   git add app.py build.py vercel.json static/app.js static/index.html static/style.css test_raileta.py test_live_provider.py README.md .env.example
   git commit -m "Remove demo and keep live train data"
   git push origin main
   ```
2. Open your existing Vercel project → **Environment Variables**. Add the variables below for **Production** (and Preview if you use preview deployments). Use a new replacement key if your original key was shared in chat. Do not include quotes or a backslash in the value.

   | Name | Value |
   | --- | --- |
   | `RAILRADAR_API_KEY` | Your private RailRadar key |
   | `RAILRADAR_BASE_URL` | `https://api.railradar.in/v1` |
   | `LIVE_POLL_SECONDS` | `60` |

3. Keep **Framework Preset: Flask**, **Root Directory: `.`** for this repository, **Build Command: `python build.py`**, and the framework's default output directory. Keep the existing homepage routing fix; add no `/` → `/index.html` rewrite.
4. Open **Deployments**, select the newest deployment from your pushed commit, and choose **Redeploy** after saving the environment variables. Environment changes require a new deployment.
5. Open the website. It should say **LIVE DATA · RAILRADAR**. Enter any supported five-digit train number. Leave the journey date empty to let RailRadar choose the current run, or select the **date the train departed its origin**, which can be yesterday for an overnight journey.
6. Check that the train name, journey date, observation age and **RAILRADAR ETA** are shown. `+1` means the day after origin departure, in IST. A dash means the provider did not supply an ETA. An amber stale/unavailable message means this is not a fresh observation.

For a quick check, `/api/config` should return `data_mode: "LIVE"` and `live_configured: true`. `/api/train/12301/live?mode=LIVE` should return `provider: "RailRadar"`, `observed_at`, `fetched_at`, and the provider's stops. A configured key alone does not prove that the provider accepted it; the train request verifies authentication. Invalid or missing keys produce safe errors, never simulated fallback data.

### Local live setup

Use Python 3.10+ and install Flask with `python3 -m pip install flask` (inside a virtual environment if needed). On Linux/macOS, run the following from this repository; the key is entered invisibly and not placed in shell history:

```sh
read -rsp 'RailRadar API key: ' RAILRADAR_API_KEY
export RAILRADAR_API_KEY
export LIVE_POLL_SECONDS=60
python3 app.py
```

Then open `http://localhost:5000`. `.env.example` documents the settings but is **not automatically loaded**; no dotenv package is required. On other systems, set the same variables through the OS or terminal environment before starting Flask. Do not commit actual `.env` files. The app always uses live data. Old `RAILETA_DATA_MODE` settings are ignored and `?mode=DEMO` requests are rejected.

### Live data behavior and limits

The browser calls this Flask backend for only the selected train. Python sends a Bearer-authenticated request to `GET https://api.railradar.in/v1/trains/{number}/live?includeCoordinates=true`, optionally with `date=YYYY-MM-DD`. That response contains the current timetable, so a second schedule call is unnecessary. See [RailRadar documentation](https://railradar.in/docs), [live status](https://railradar.in/docs/live-train-status), [train details](https://railradar.in/docs/get-train-details), and [provider terms](https://railradar.in/terms).

- `data_mode`, `provider`, `journey_status`, `journey_start_date`: source and identity of this journey, supplied by the provider.
- `observed_at`: provider `lastUpdatedAt`; `fetched_at`: successful server fetch time. Missing, timezone-free, future or inconsistent observation timestamps are treated as unavailable. Observations older than five minutes are marked stale. That threshold is an application policy, not a provider freshness guarantee.
- `stops[].actual_arr` / `actual_dep`: explicitly reported station events, accepted only for suitable arrived/departed statuses and times no later than the observation. Earlier stations are not automatically assigned actual events.
- `stops[].predicted_eta`: RailRadar projection, with `eta_source` and `eta_basis`. In the authenticated response, RailRadar places future projections in `actualArrival` on **upcoming** rows. The adapter treats only those future values as projections, never completed arrivals. No synthetic predictor runs in live mode. Missing projections stay null. `baseline_eta` is a separate scheduled-arrival-plus-current-delay comparison.
- `position.source`: `PROVIDER_REPORTED` for supplied coordinates, `ESTIMATED` when only the provider-named station's coordinates are available, otherwise `UNAVAILABLE`. There is no claim of measured GPS or invented accuracy/speed. Missing values stay null.

Live mode refreshes every 60 seconds by default, pauses polling while hidden, prevents overlapping browser requests, and offers manual refresh. It honors retry delays and backs off after failures. The last good result remains visible but is marked stale when a refresh fails. There is no data-mode selector, simulation control, or synthetic accuracy panel.

The bounded in-memory cache coalesces repeated requests **only within one Python worker**. It does not enforce a global quota across Vercel instances. One continuously visible page can consume roughly 60 provider requests per hour at a 60-second interval. Check your RailRadar plan's current allowance, and add shared caching and rate limiting before public scale. No database or paid service has been added. Third-party status can be estimated, delayed or unavailable; this app has no affiliation with Indian Railways, CRIS, RTIS or NTES.

### Verification performed

- **Offline:** Live-provider and live-only application tests, covering normalization, overnight ETAs, missing/stale data, journey statuses, HTTP errors, retries, caching, rejection of demo requests, removed simulation endpoints, asset routes and secret redaction. Run `python3 -m unittest -v test_raileta test_live_provider`.
- **Authenticated:** On 23 September 2026, the supplied key successfully retrieved train **12301** through the new backend. The browser displayed RailRadar's current route with nine halts, observation age, location and next-day destination ETA. This confirms that particular provider integration and journey, not universal coverage, accuracy or GPS provenance.
- **Deployment:** These changes are local until committed, pushed and redeployed with the variables above. No production deployment was performed as part of this change.

## Application and build checks

```sh
python3 -m unittest -v test_raileta test_live_provider
node --check static/app.js
python3 build.py
```

`build.py` stages frontend files in ignored `public/` for Vercel. It does not need generated demo data. The Flask app imports only the live provider adapter; the existing offline research scripts and datasets are not loaded or exposed by the website. `/api/sim`, `/api/conditions`, and `/api/evaluation` now return 404. No replay sessions or simulation cookies are created.
