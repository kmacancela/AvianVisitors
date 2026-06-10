import type { Config } from "@netlify/functions";
import { getDeployStore, getStore } from "@netlify/blobs";
import { createHash, timingSafeEqual } from "node:crypto";

type Snapshot = Record<string, any>;

declare const Netlify: {
  context?: { deploy?: { context?: string } };
};

const AUDIO_KEY_RE = /^public-audio\/[a-z0-9-]+\/[a-f0-9]{24}\.mp3$/;
const SCI_RE = /^[A-Za-z]{2,40}(?:[ ][a-z]{2,40}){1,3}$/;
const COOKIE = "avian_audio_session";

function store() {
  if (Netlify.context?.deploy?.context === "production") {
    return getStore({ name: "avianvisitors", consistency: "strong" });
  }
  return getDeployStore("avianvisitors");
}

async function seedSnapshot(req: Request): Promise<Snapshot | null> {
  const seedUrl = new URL("/data/seed-snapshot.json", req.url);
  const response = await fetch(seedUrl);
  if (!response.ok) return null;
  return response.json();
}

async function loadSnapshot(req: Request): Promise<Snapshot | null> {
  const saved = await store().get("snapshot", { type: "json" });
  return saved ?? seedSnapshot(req);
}

function audioFor(snapshot: Snapshot, sci: string) {
  const detail = snapshot.species?.[sci];
  const fromDetail = detail?.public_audio || detail?.summary?.public_audio;
  if (fromDetail) return fromDetail;
  const row = ((snapshot.lifelist?.species || []) as Array<Record<string, any>>)
    .find((item) => item.sci === sci);
  return row?.public_audio || null;
}

function text(message: string, status = 404) {
  return new Response(message, {
    status,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function password() {
  return process.env.AVIAN_PUBLIC_PASSWORD || "";
}

function token() {
  return createHash("sha256").update(`avian-public-audio:${password()}`).digest("hex");
}

function safeEqual(a: string, b: string) {
  const aa = Buffer.from(a);
  const bb = Buffer.from(b);
  return aa.length === bb.length && timingSafeEqual(aa, bb);
}

function cookie(req: Request) {
  const raw = req.headers.get("cookie") || "";
  const found = raw.split(/;\s*/).find((part) => part.startsWith(`${COOKIE}=`));
  return found ? decodeURIComponent(found.slice(COOKIE.length + 1)) : "";
}

function hasAudioSession(req: Request) {
  const expected = password();
  return !!expected && safeEqual(cookie(req), token());
}

export default async (req: Request) => {
  if (req.method !== "GET" && req.method !== "HEAD") {
    return text("method not allowed", 405);
  }

  if (!hasAudioSession(req)) return text("unauthorized", 401);

  const url = new URL(req.url);
  const sci = (url.searchParams.get("sci") || "").trim();
  if (!SCI_RE.test(sci)) return text(sci ? "invalid sci" : "sci required", sci ? 400 : 404);

  const snapshot = await loadSnapshot(req);
  const audio = snapshot ? audioFor(snapshot, sci) : null;
  const key = audio?.key || "";
  if (!AUDIO_KEY_RE.test(key)) return text("no public recording for species", 404);

  const data = await store().get(key, { type: "arrayBuffer" });
  if (!data) return text("recording not found", 404);

  return new Response(req.method === "HEAD" ? null : data, {
    status: 200,
    headers: {
      "content-type": audio.content_type || "audio/mpeg",
      "content-length": String(data.byteLength),
      "cache-control": "public, max-age=300",
      "accept-ranges": "bytes",
    },
  });
};

export const config: Config = {
  path: "/avian/api/recording.php",
};
