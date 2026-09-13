# Bulgarian Natural Gas Regulated Pricing

Home Assistant integration that provides the current regulated price of natural
gas for Bulgarian household customers supplied by **Овергаз Мрежи** — in
**EUR per cubic metre**, ready to drop straight into the Energy dashboard.

Интеграция за Home Assistant, която предоставя текущата регулирана цена на
природния газ за битови клиенти на „Овергаз Мрежи“ в евро за кубичен метър,
готова за използване в Energy таблото.

## Why this exists

Bulgarian gas meters measure **cubic metres**, but gas has been *priced* in
**MWh** since December 2024. Converting between them needs two figures that are
published separately, monthly, by two different organisations:

| Figure | Published by | When |
| --- | --- | --- |
| Tariff, EUR/MWh excluding VAT | Овергаз Мрежи | on or just before the 1st |
| Representative calorific value, kWh/m³ | Булгартрансгаз | ~15 days before the month |

Neither is available as an API. This integration reads both from the documents
the two operators publish, applies VAT, and gives you a single price sensor.

```
price [EUR/m³] = (Σ tariff components) × (1 + VAT) ÷ 1000 × calorific value
```

## Installation

### HACS (custom repository)

1. HACS → ⋮ → **Custom repositories**
2. Repository: `https://github.com/Teodor92/bg_gas_regulated_pricing`, category **Integration**
3. Install, then restart Home Assistant
4. **Settings → Devices & services → Add integration → Bulgarian Natural Gas Regulated Pricing**

### Manual

Copy `custom_components/bg_gas_regulated_pricing` into your `config/custom_components`
directory and restart.

## Configuration

Pick the supply area your home is connected to:

| Area | Notes |
| --- | --- |
| Sofia and other Overgas Mrezhi areas | the standard tariff |
| Bansko and Razlog | CNG-supplied, carries a compression surcharge |
| Byala | CNG-supplied |
| Karnobat | CNG-supplied |

The VAT rate defaults to 20%, which is what Bulgarian households pay. It can be
changed later from the integration's options.

## Entities

| Entity | Unit | Notes |
| --- | --- | --- |
| `sensor.…_price` | EUR/m³ | the one to use in the Energy dashboard |
| `sensor.…_price_per_kwh` | EUR/kWh | |
| `sensor.…_price_per_mwh` | EUR/MWh | matches the figure quoted in the news |
| `sensor.…_price_per_mwh_excluding_vat` | EUR/MWh | diagnostic; the published net tariff |
| `sensor.…_calorific_value` | kWh/m³ | diagnostic; this month's conversion factor |

The price sensor carries the full tariff breakdown in its attributes —
distribution, supply, transmission and access, delivery, and compression where
it applies — along with the period and the URL each figure came from.

## Using it in the Energy dashboard

**Settings → Dashboards → Energy → Gas consumption**, add your m³ gas sensor and
set **Use an entity tracking the total costs** → *Use an entity with the current
price* → `sensor.…_price`.

Your gas meter sensor must be in **m³**. If it reports kWh instead, use
`sensor.…_price_per_kwh`.

## How it decides what to trust

A wrong price is worse than a missing one: the Energy dashboard multiplies it by
consumption as that accrues, so a bad figure quietly corrupts cost history in a
way that is painful to unwind. The integration therefore:

- **verifies the tariff adds up** — the last column of the published table is the
  end price and the preceding columns are its components, so the parser checks
  they sum correctly rather than assuming a fixed column count (the CNG areas
  have an extra column, and Overgas has changed this before);
- **cross-checks the currency** — during the euro transition the same tariff is
  published twice, in euro and in legacy lev, and the two are confirmed against
  the fixed 1.95583 rate before the euro row is used;
- **bounds every figure** — a tariff outside 20–400 EUR/MWh or a calorific value
  outside 9–12 kWh/m³ is rejected as a parse failure;
- **holds the last known-good value** if a source is unreachable, rather than
  going unavailable and leaving consumption uncosted;
- **raises a repair issue** if it is still serving a previous month's tariff more
  than three days into a new month.

## Sources

- [Овергаз Мрежи — Цени на природния газ](https://www.overgas.bg/za-overgaz/produkti-i-uslugi/tseni-na-prirodniya-gaz/)
- [Булгартрансгаз — Представителна калоричност](https://bulgartransgaz.bg/pages/sertifikat-46.html)
- [Булгартрансгаз — Методика за превръщане в енергийни единици](https://bulgartransgaz.bg/files/useruploads/files/GDU/methodology_GCV.pdf)

Both are scraped from published documents, not an API, so the format can change
without notice. If it does, the integration will hold its last value and raise a
repair issue — please open an issue here as well.

## Licence

Apache License 2.0
