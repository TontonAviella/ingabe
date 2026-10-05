<h1 align="center">Ingabe</h1>

<p align="center"><b>Drone and satellite crop intelligence for Rwanda, with Sage.</b></p>

<p align="center">
  <a href="https://github.com/TontonAviella/ingabe/actions/workflows/cicd.yml"><img src="https://img.shields.io/github/actions/workflow/status/TontonAviella/ingabe/cicd.yml?branch=main&label=CI" alt="CI" /></a>
  <a href="https://github.com/TontonAviella/ingabe/actions/workflows/lint.yml"><img src="https://img.shields.io/github/actions/workflow/status/TontonAviella/ingabe/lint.yml?branch=main&label=lint" alt="Lint" /></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue" alt="License: AGPL-3.0" /></a>
</p>

Ingabe turns drone images, satellite data and weather into plain answers for the people who decide on Rwanda's fields: **farmers, insurers, agronomists and agricultural scientists**. You upload a drone flight or a field boundary and ask **Sage**, Ingabe's assistant, what you want to know. Sage finds the right data, puts it on the map, and answers in words, for your role.

## What it does

- **Drone images first.** Upload an orthophoto and Sage posts a first look (greenness and crop cover). Ask for more and it finds stressed patches, compares two flights, and scores an insurance trigger from NDVI flights.
- **Vegetation, drought and crop risk.** NDVI and other Sentinel-2 indices for any district, sector or cell; drought, vegetation alerts, crop growth stage and yield-risk trends per district.
- **Weather and insurance.** Past rainfall (CHIRPS, AgERA5), a four-model 16-day forecast, dry spells, and season reports with trigger status for insurers.
- **Soil and water.** iSDAsoil properties, FAO WaPOR soil moisture and evapotranspiration, and Sentinel-1 radar through clouds (NDVI estimates, flood extent).
- **Rwanda built in.** All districts, sectors, cells and villages, with outlines that follow the zoom.
- **A memory for your organization.** The Brain stores reports, policies and field records, so Sage can read and quote them.

Data sources and update times are in the [training manual](docs/TRAINING_MANUAL.md#17-data-sources-and-update-times).

## Running it locally

Ingabe runs on your own machine with Docker Compose.

1. Install Docker and git, then clone this repository.
2. Copy the settings template and fill it in:
   ```bash
   cp .env.example .env
   ```
   - **Sage** needs an LLM key: `OPENAI_API_KEY` with `OPENAI_BASE_URL` (OpenRouter by default).
   - **Sign-in** uses WorkOS: the `WORKOS_*` settings.
3. Build, start and check everything:
   ```bash
   scripts/deploy.sh
   ```
4. Open [http://localhost:8000](http://localhost:8000).

`scripts/deploy.sh --check-only` re-checks a running stack without rebuilding it. Agents and contributors: start with [AGENTS.md](AGENTS.md) and [CODING_STANDARDS.md](CODING_STANDARDS.md).

## Documentation

- [Training manual](docs/TRAINING_MANUAL.md): using Ingabe and Sage, for every role.
- [Local runtime architecture](docs/LOCAL_RUNTIME_ARCHITECTURE.md)

## Security

Please don't report security problems in a public issue. Contact the maintainer privately through [GitHub](https://github.com/TontonAviella).

## License and origin

Ingabe is licensed under the [GNU Affero General Public License v3](./LICENSE). It began as a fork of [Mundi](https://github.com/BuntingLabs/mundi.ai) by Bunting Labs, also AGPL-3.0, and has since been extended and adapted for agriculture in Rwanda.

Developed by [NozaLabs](https://app.nozalabs.rw).
