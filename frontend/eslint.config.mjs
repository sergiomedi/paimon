import coreWebVitals from "eslint-config-next/core-web-vitals";
import typescript from "eslint-config-next/typescript";

/**
 * Flat config. eslint-config-next ships its presets as flat configs, so no
 * FlatCompat shim is needed.
 */
const config = [
  ...coreWebVitals,
  ...typescript,
  {
    // eslint-plugin-react's automatic detection resolves the React package from
    // the linted file's directory, which fails under pnpm's non-flat
    // node_modules. Stating the version is both cheaper and deterministic.
    settings: { react: { version: "19.2" } },
  },
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
];

export default config;
