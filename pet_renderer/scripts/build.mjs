import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const entryPoint = fileURLToPath(new URL("../src/main.js", import.meta.url));
const output = fileURLToPath(new URL("../../bongo/assets/bongocat/renderer.js", import.meta.url));

await build({
  entryPoints: [entryPoint],
  bundle: true,
  minify: true,
  format: "iife",
  platform: "browser",
  target: "chrome120",
  outfile: output,
});

const bundled = await readFile(output, "utf8");
await writeFile(output, bundled.replace(/[ \t]+$/gm, ""), "utf8");
