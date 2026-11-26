// Writes the old engine's calculation settings on a given basis, built by the app's own code from one exported app
// commit, so the post types are exactly the ones its default run uses. `anbud baseline` runs it with bun, under the
// baseline preload in dry-run mode, so it can make no network call.
//
//   bun baseline_settings.mjs <exported app root> <out.json>      (the basis as JSON in ANBUD_BASIS)
import { writeFileSync } from 'node:fs';

const [app, out] = process.argv.slice(2);
const basis = JSON.parse(process.env.ANBUD_BASIS ?? 'null');
if (!app || !out || !basis) throw Error('usage: baseline_settings.mjs <app root> <out.json>, with ANBUD_BASIS set');
const { emptyCalculationSettings, validateSettingsInput } = await import(`${app}/src/lib/tender-calculation.ts`);
const { withExamplePostTypes } = await import(`${app}/src/lib/tender-example-catalog.ts`);
const settings = validateSettingsInput(withExamplePostTypes({ ...emptyCalculationSettings, ...basis, example_values: true }));
for (const [key, value] of Object.entries(basis)) {
  if (settings[key] !== value) throw Error(`the app changed ${key}`);
}
writeFileSync(out, JSON.stringify(settings, null, 1), { mode: 0o600 });
