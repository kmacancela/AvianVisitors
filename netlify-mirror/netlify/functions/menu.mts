import type { Config } from "@netlify/functions";
import { createHash, timingSafeEqual } from "node:crypto";

const COOKIE = "avian_audio_session";
const MAX_AGE = 60 * 60 * 24 * 30;

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

function credentials(req: Request) {
  const auth = req.headers.get("authorization") || "";
  const m = auth.match(/^Basic\s+(.+)$/i);
  if (!m) return ["", ""];
  const decoded = Buffer.from(m[1], "base64").toString("utf8");
  const i = decoded.indexOf(":");
  return i === -1 ? ["", ""] : [decoded.slice(0, i), decoded.slice(i + 1)];
}

function json(body: Record<string, unknown>, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      ...headers,
    },
  });
}

export default async (req: Request) => {
  const expected = password();
  if (!expected) return json({ error: "auth not configured" }, 503);

  const expectedToken = token();
  if (safeEqual(cookie(req), expectedToken)) return json({ items: [], audio: true });

  const [user, pass] = credentials(req);
  if (user === "birdnet" && pass === expected) {
    return json({ items: [], audio: true }, 200, {
      "set-cookie": `${COOKIE}=${encodeURIComponent(expectedToken)}; Path=/; Max-Age=${MAX_AGE}; HttpOnly; SameSite=Lax; Secure`,
    });
  }

  return json({ error: "unauthorized" }, 401);
};

export const config: Config = {
  path: "/avian/api/menu.php",
};
