# Deadlock Match Intelligence

**An unofficial, experimental Windows tool for exploring Deadlock player performance, match statistics, and possible smurfing indicators.**

> **Early development — v0.5.8 (beta).** Results are heuristic screening signals, **not proof that a player is smurfing**. The tool is not affiliated with, sponsored by, or endorsed by Valve.

## What it does

Deadlock Match Intelligence (application title: **Deadlock Profile Analyzer**) combines available public player and match data with performance comparisons to help identify accounts that may warrant closer review. It places particular emphasis on **farming efficiency**, since efficient lane and jungle farming can reflect game knowledge and experience.

### Features

- **Profile Dashboard:** Review recent available match performance, including farming, KDA, accuracy, critical-shot rate, and screening indicators.
- **Smurf screening (0–100):** A provisional, farming-focused score informed by the data actually available. Higher scores indicate more unusual performance, **not a probability of cheating or smurfing**.
- **Separate evidence confidence:** Shows how much data supports an assessment. A high screening score can coexist with **Low** confidence when match history is sparse or lifetime experience is unknown.
- **Match Lookup:** Inspect the players in a match, hero names, available match statistics, and souls per minute. Above-general-benchmark souls/min is highlighted for easier comparison. Player text and match identifiers can be selected and copied.
- **Farming comparisons:** Compare observed souls/min to players in the same matches and, when available, to the API's general single-match benchmark distribution.
- **Rank Benchmarks:** Explore available rank-filtered analytics as an optional feature; rank information may be incomplete or unavailable and is **not required** for general screening.
- **Raw Report:** View and copy a detailed text report, or generate a shorter report description for manual review.
- **Instant explanatory tooltips:** Hover over unfamiliar metrics to understand how they are calculated and what their limitations are.
- **Built-in Console:** Review diagnostics within the application, without requiring an external console window.
- **Automatic benchmark refresh and local cache:** Previously retrieved general benchmarks remain usable when the API is unavailable.

## Download and run on Windows

**For most users:** Download the Windows executable from this repository's **[Releases](../../releases)** page when a release is available.

1. Download `Deadlock Profile Analyzer.exe` from the release assets.
2. Run the executable. **Python is not required.**
3. Enter a supported Steam profile identifier or Deadlock account ID, or use **Match Lookup** to inspect a match.
4. Allow the general benchmark to load in the background. Early analyses may use cached benchmark data or omit benchmark-specific comparisons until the download completes.

An internet connection is required to retrieve live data. Windows may display a security or reputation warning for an unsigned or newly distributed build; verify the download source and do not disable antivirus protection to run it.

## Build it yourself

The repository includes a Windows build script and the resources it uses:

```text
deadlock-match-intelligence/
├── deadlock_profile_analyzer_0_5_8.py
├── build.bat
├── deadlock_analyzer.ico
└── version_info.txt
```

On a **Windows development computer**:

1. Install Python 3 (including `pip`).
2. Place the four files above in the same folder.
3. Double-click **`build.bat`**. The script installs PyInstaller if it is missing and builds the application.
4. Find the standalone executable at `dist\Deadlock Profile Analyzer.exe`.

The build requires Python; the resulting executable does not. PyInstaller creates local `build/` and `dist/` folders, which do not need to be committed to the repository. The generated EXE is not automatically code-signed.

Alternatively, run the Python source directly:

```powershell
py deadlock_profile_analyzer_0_5_8.py
```

The application uses Python's Tkinter GUI toolkit and standard-library HTTP tools; the build process uses PyInstaller.

## Understanding the scores

| Screening level | Score |
| --- | ---: |
| Low | 0–29 |
| Moderate | 30–54 |
| Elevated | 55–74 |
| High | 75–100 |

**Screening** describes how unusual or experienced the available gameplay appears under the application's experimental scoring rules. **Confidence** describes how complete and consistent the supporting observations are. Confidence does not directly lower the screening score.

Farming proficiency is weighted heavily, alongside available experience mismatch, combat performance, mechanical statistics, and consistency. The weights and thresholds are **provisional**, not validated probability estimates. A skilled new player, someone with experience in similar games, or an unusual match can generate a high score without being a smurf.

### Important data limitations

- **Available matches are not lifetime matches.** A small returned history does not prove an account is new or has few hours.
- **Unknown playtime stays unknown.** The analyzer does not assume zero hours when verified Deadlock playtime is unavailable. Dota 2 playtime is not used.
- **General benchmarks are sampled, not universal.** The analytics endpoint can be requested with `max_matches=500`; this is not a census of every player or every game.
- **Benchmark percentiles describe single player-match observations, not a player's lifetime percentile.** The same person may contribute multiple observations.
- **Peer comparisons depend on available match records.** They are not necessarily fair rank-, hero-, or role-matched comparisons.
- **API availability and coverage can change.** Rank-related data in particular may be limited. Some metrics can be missing or calculated differently across heroes or patches.

Use the tool to **flag accounts for human review**, not to publish accusations or treat a score as conclusive evidence of a rules violation.

## Local data and logs

On Windows, the application stores cache and diagnostic files under:

```text
%LOCALAPPDATA%\DeadlockProfileAnalyzer\
├── general_benchmark.json
└── logs\
    └── analyzer.log
```

Existing benchmark caches from older versions may be migrated automatically. **Do not upload personal logs, API credentials, or user-specific cache files to a public repository.**

## Data sources and attribution

The application retrieves available data from the community-operated [Deadlock API](https://deadlock-api.com/) and related public Steam profile endpoints. It is not an official Valve application. API schemas, access restrictions, caching, and rate limits are controlled by their respective providers and may change.

*Deadlock* and associated marks belong to their respective owners. Project artwork, dependencies, and any third-party assets remain subject to their own applicable licenses.

## Project status and contributions

This is an **early-stage experimental project**. Bug reports, reproducible API issues, feedback on unclear metrics, and suggestions for better screening methodology are welcome via GitHub Issues. Please avoid posting personal information or publicly accusing individual players based solely on a screening score.

### Development disclosure

This project was developed with **AI-assisted programming**, directed, tested, and iterated on by its maintainer. AI assistance does not replace real-world testing, code review, or validation of screening accuracy.

## License

**No open-source license has been selected yet.** Until one is added, the source is publicly viewable on GitHub, but that does not automatically grant others permission to redistribute or modify it.
