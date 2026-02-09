import fs from "fs";
import path from "path";
import { chromium } from "playwright";
import { fileURLToPath } from "url";
import { createWriteStream } from "fs";
import { pipeline } from "stream/promises";
import { createRequire } from "module";
import { setTimeout as delay } from "timers/promises";
import zlib from "zlib";

// ---------- small ZIP (CBZ) writer without extra deps ----------
/**
 * Minimal ZIP writer: stores files (no compression) to create .cbz
 * Works for typical comic readers.
 */
function crc32(buf) {
  // fast enough crc32 (table)
  const table = crc32.table || (crc32.table = (() => {
    const t = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      t[i] = c >>> 0;
    }
    return t;
  })());

  let c = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) c = table[(c ^ buf[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}

function u16(n) {
  const b = Buffer.alloc(2);
  b.writeUInt16LE(n);
  return b;
}
function u32(n) {
  const b = Buffer.alloc(4);
  b.writeUInt32LE(n >>> 0);
  return b;
}

async function createCBZ(cbzPath, files) {
  // "files" = [{nameInZip, absPath}]
  const out = createWriteStream(cbzPath);
  const central = [];
  let offset = 0;

  for (const f of files) {
    const data = await fs.promises.readFile(f.absPath);
    const nameBuf = Buffer.from(f.nameInZip, "utf8");
    const crc = crc32(data);

    // Local file header
    // 0x04034b50
    const localHeader = Buffer.concat([
      u32(0x04034b50),
      u16(20),        // version needed
      u16(0),         // flags
      u16(0),         // compression 0 = store
      u16(0), u16(0), // time/date
      u32(crc),
      u32(data.length),
      u32(data.length),
      u16(nameBuf.length),
      u16(0)          // extra len
    ]);

    await new Promise((res, rej) => out.write(localHeader, err => err ? rej(err) : res()));
    await new Promise((res, rej) => out.write(nameBuf, err => err ? rej(err) : res()));
    await new Promise((res, rej) => out.write(data, err => err ? rej(err) : res()));

    const localOffset = offset;
    offset += localHeader.length + nameBuf.length + data.length;

    // Central directory header
    // 0x02014b50
    const centralHeader = Buffer.concat([
      u32(0x02014b50),
      u16(20),        // made by
      u16(20),        // version needed
      u16(0),         // flags
      u16(0),         // compression
      u16(0), u16(0), // time/date
      u32(crc),
      u32(data.length),
      u32(data.length),
      u16(nameBuf.length),
      u16(0),         // extra
      u16(0),         // comment
      u16(0),         // disk number
      u16(0),         // internal attrs
      u32(0),         // external attrs
      u32(localOffset)
    ]);

    central.push({ centralHeader, nameBuf });
  }

  const centralStart = offset;
  for (const c of central) {
    await new Promise((res, rej) => out.write(c.centralHeader, err => err ? rej(err) : res()));
    await new Promise((res, rej) => out.write(c.nameBuf, err => err ? rej(err) : res()));
    offset += c.centralHeader.length + c.nameBuf.length;
  }

  const centralSize = offset - centralStart;

  // End of central directory
  // 0x06054b50
  const eocd = Buffer.concat([
    u32(0x06054b50),
    u16(0), u16(0),
    u16(central.length),
    u16(central.length),
    u32(centralSize),
    u32(centralStart),
    u16(0)
  ]);

  await new Promise((res, rej) => out.write(eocd, err => err ? rej(err) : res()));
  await new Promise((res) => out.end(res));
}

// ---------- helpers ----------
function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
}

function safeName(s) {
  return String(s || "").trim().replace(/[^\w.-]+/g, "_").replace(/^_+|_+$/g, "") || "series";
}

function guessExtFromUrl(u) {
  try {
    const urlObj = new URL(u);
    const ext = path.extname(urlObj.pathname).toLowerCase();
    if ([".jpg", ".jpeg", ".png", ".webp", ".gif"].includes(ext)) return ext;
  } catch {}
  return ".jpg";
}

async function downloadFile(url, dest, headers = {}, referer = "") {
  // Node 18+ fetch
  const h = new Headers(headers);
  if (referer) h.set("referer", referer);

  const res = await fetch(url, { headers: h });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);

  const file = createWriteStream(dest);
  await pipeline(res.body, file);
}

function unique(arr) {
  const seen = new Set();
  const out = [];
  for (const x of arr) {
    if (!x) continue;
    if (seen.has(x)) continue;
    seen.add(x);
    out.push(x);
  }
  return out;
}

async function mapLimit(items, limit, fn) {
  const results = new Array(items.length);
  let i = 0;
  const workers = new Array(Math.min(limit, items.length)).fill(0).map(async () => {
    while (true) {
      const idx = i++;
      if (idx >= items.length) break;
      results[idx] = await fn(items[idx], idx);
    }
  });
  await Promise.all(workers);
  return results;
}

// ---------- main scraping (authorized only) ----------
async function run(configPath) {
  const cfg = JSON.parse(fs.readFileSync(configPath, "utf8"));

  ensureDir(cfg.outputDir);

  const browser = await chromium.launch({ headless: cfg.browser?.headless ?? true });
  const context = await browser.newContext({
    userAgent: cfg.headers?.userAgent || "manga-dl-js/1.0",
  });
  const page = await context.newPage();

  console.log("Opening series:", cfg.seriesUrl);
  await page.goto(cfg.seriesUrl, { waitUntil: "domcontentloaded" });

  // Let JS populate chapter list
  await page.waitForLoadState("networkidle").catch(() => {});

  const chapterLinks = await page.$$eval(
    cfg.selectors.chapterLinks,
    (els, attr) => els.map(el => el.getAttribute(attr)).filter(Boolean),
    cfg.selectors.chapterHrefAttr || "href"
  );

  const base = new URL(cfg.seriesUrl);
  let chaptersAbs = unique(chapterLinks.map(href => new URL(href, base).toString()));

  if (cfg.chapterOrder?.reverse) chaptersAbs = chaptersAbs.reverse();

  if (!chaptersAbs.length) {
    throw new Error("No chapters found. Check selectors.chapterLinks / chapterHrefAttr.");
  }

  console.log(`Found ${chaptersAbs.length} chapter(s)`);

  const seriesDir = path.join(cfg.outputDir, "series");
  ensureDir(seriesDir);

  // process chapters with limited concurrency (separate pages)
  await mapLimit(chaptersAbs, cfg.concurrency || 3, async (chUrl, chIndex) => {
    const chNum = chIndex + 1;
    const chLabel = `Chapter_${String(chNum).padStart(3, "0")}`;
    console.log(`\n[${chNum}/${chaptersAbs.length}] ${chUrl}`);

    const chPage = await context.newPage();
    try {
      await chPage.goto(chUrl, { waitUntil: "domcontentloaded" });
      // allow JS to render images
      await chPage.waitForLoadState("networkidle", { timeout: cfg.wait?.imagesTimeoutMs ?? 30000 }).catch(() => {});
      if (cfg.wait?.chapterNetworkIdleMs) await delay(cfg.wait.chapterNetworkIdleMs);

      // Wait until we have some images
      const minCount = cfg.wait?.imagesMinCount ?? 1;
      const timeoutMs = cfg.wait?.imagesTimeoutMs ?? 30000;

      const start = Date.now();
      while (true) {
        const count = await chPage.$$eval(cfg.selectors.pageImages, els => els.length).catch(() => 0);
        if (count >= minCount) break;
        if (Date.now() - start > timeoutMs) break;
        await delay(250);
      }

      const imgUrls = await chPage.$$eval(
        cfg.selectors.pageImages,
        (els, attrCandidates) => {
          const out = [];
          for (const el of els) {
            let val = "";
            for (const a of attrCandidates) {
              const v = el.getAttribute(a);
              if (v && v.trim()) { val = v.trim(); break; }
            }
            if (val) out.push(val);
          }
          return out;
        },
        cfg.selectors.imageAttrCandidates || ["src", "data-src"]
      );

      const imgAbs = unique(imgUrls.map(u => new URL(u, chUrl).toString()));
      console.log(`  - found ${imgAbs.length} image(s)`);

      if (!imgAbs.length) {
        console.log("  - skip: no images (selector wrong or JS loads via canvas/api)");
        return;
      }

      const tmpDir = path.join(seriesDir, `tmp_${chLabel}`);
      ensureDir(tmpDir);

      // download sequentially named
      const headerUA = cfg.headers?.userAgent ? { "user-agent": cfg.headers.userAgent } : {};
      const referer = cfg.headers?.referer || chUrl;

      const files = await mapLimit(imgAbs, cfg.concurrency || 3, async (imgUrl, i) => {
        const ext = guessExtFromUrl(imgUrl);
        const name = `${String(i + 1).padStart(3, "0")}${ext}`;
        const dest = path.join(tmpDir, name);

        // resume
        if (fs.existsSync(dest)) return { nameInZip: name, absPath: dest };

        await downloadFile(imgUrl, dest, headerUA, referer);
        return { nameInZip: name, absPath: dest };
      });

      // Sort by name in zip
      files.sort((a, b) => a.nameInZip.localeCompare(b.nameInZip));

      const cbzPath = path.join(seriesDir, `${chLabel}.cbz`);
      await createCBZ(cbzPath, files);
      console.log(`  - wrote ${cbzPath}`);

      // cleanup
      fs.rmSync(tmpDir, { recursive: true, force: true });
    } finally {
      await chPage.close().catch(() => {});
    }
  });

  await browser.close();
  console.log("\nDone.");
}

// entry
const cfgPath = process.argv[2];
if (!cfgPath) {
  console.error("Usage: node index.js config.json");
  process.exit(2);
}
run(cfgPath).catch(err => {
  console.error("Error:", err?.stack || err);
  process.exit(1);
});