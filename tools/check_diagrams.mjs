/**
 * Parse every mermaid block in the docs, so a broken diagram fails the build
 * rather than rendering as an error box on GitHub.
 *
 * Mermaid expects a browser, so jsdom stands in for one. Only the parser runs:
 * nothing is laid out or drawn, which keeps this a syntax check and keeps
 * chromium out of CI.
 *
 *     node tools/check_diagrams.mjs README.md ARCHITECTURE.md
 */

import fs from "fs";
import path from "path";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true });
global.window = dom.window;
global.document = dom.window.document;
Object.defineProperty(global, "navigator", { value: dom.window.navigator, configurable: true });

const mermaid = (await import("mermaid")).default;
mermaid.initialize({ startOnLoad: false, securityLevel: "loose" });

const files = process.argv.slice(2);
if (files.length === 0) {
  console.error("usage: node tools/check_diagrams.mjs <file.md> [...]");
  process.exit(2);
}

let blocks = 0;
let failures = 0;

for (const file of files) {
  const markdown = fs.readFileSync(file, "utf8");
  const found = [...markdown.matchAll(/```mermaid\r?\n([\s\S]*?)```/g)];

  for (const [index, match] of found.entries()) {
    blocks++;
    const kind = match[1].trim().split("\n")[0].trim();
    const label = `${path.basename(file)} block ${index + 1} (${kind})`;
    try {
      await mermaid.parse(match[1]);
      console.log(`  ok    ${label}`);
    } catch (error) {
      failures++;
      const reason = String(error.message).split("\n").slice(0, 4).join(" | ");
      console.log(`  FAIL  ${label}: ${reason}`);
    }
  }
}

console.log(
  failures
    ? `\n${failures} of ${blocks} diagram(s) failed to parse`
    : `\n${blocks} diagram(s) parse`
);
process.exit(failures ? 1 : 0);
