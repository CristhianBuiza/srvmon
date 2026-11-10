// Regenerates the README images from a dashboard HTML file.
//
//   srvmon html --out dashboard.html && srvmon report > report.txt
//   npm i playwright-core && npx playwright install chromium   (or set CHROME=/path/to/chrome)
//   node tools/screenshots.js dashboard.html report.txt docs/images
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright-core");

const [htmlFile, reportFile, outDir] = process.argv.slice(2);
if (!htmlFile || !outDir) {
  console.error("usage: node tools/screenshots.js dashboard.html [report.txt] out-dir");
  process.exit(2);
}

function terminalHtml(text) {
  const body = text.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  return `<!doctype html><meta charset="utf-8"><style>
    body { margin: 0; background: #0f1720; padding: 0; }
    .win { margin: 0; background: #111a24; color: #d6e2ee; font: 13px/1.5 ui-monospace, "DejaVu Sans Mono", Menlo, monospace; }
    .bar { background: #1c2733; padding: 9px 14px; color: #8fa3b8; font-size: 12px; }
    .bar i { display: inline-block; width: 11px; height: 11px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; }
    pre { margin: 0; padding: 16px 18px 20px; white-space: pre; }
    .p { color: #7ee787; }
  </style><div class="win"><div class="bar"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i>&nbsp; root@server: ~</div>
  <pre><span class="p">root@server:~#</span> srvmon report\n${body}</pre></div>`;
}

(async () => {
  const browser = await chromium.launch(process.env.CHROME ? { executablePath: process.env.CHROME } : {});
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 1.5 });
  const url = "file://" + path.resolve(htmlFile);
  fs.mkdirSync(outDir, { recursive: true });

  await page.goto(url);
  await page.waitForTimeout(300);
  const box = async (sel) => page.locator(sel).first().boundingBox();
  const header = await box("header");
  const week = await box("main > section.card");
  // Hover the CPU chart so the tooltip is part of the overview image.
  const cpu = await box("#w-cpu svg");
  await page.mouse.move(cpu.x + cpu.width * 0.62, cpu.y + cpu.height * 0.4);
  await page.screenshot({ path: path.join(outDir, "dashboard-overview.png"), fullPage: true,
    clip: { x: 0, y: header.y, width: 1360, height: week.y + week.height + 16 - header.y } });

  await page.mouse.move(0, 0);
  const days = await box("#days");
  const detail = await box("#day-section");
  await page.screenshot({ path: path.join(outDir, "dashboard-daily.png"), fullPage: true,
    clip: { x: 0, y: days.y - 50, width: 1360, height: detail.y + detail.height + 16 - (days.y - 50) } });

  const procs = await box("main > section.card:last-of-type");
  await page.screenshot({ path: path.join(outDir, "dashboard-processes.png"), fullPage: true,
    clip: { x: 0, y: procs.y - 16, width: 1360, height: procs.height + 32 } });

  if (reportFile) {
    await page.setViewportSize({ width: 1040, height: 600 });
    await page.setContent(terminalHtml(fs.readFileSync(reportFile, "utf8")));
    const term = await box(".win");
    await page.screenshot({ path: path.join(outDir, "report-terminal.png"), clip: term });
  }
  await browser.close();
  console.log("images written to " + outDir);
})().catch((e) => { console.error(e); process.exit(1); });
