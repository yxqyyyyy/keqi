declare namespace Cloudflare {
  interface Env {
    DB?: D1Database;
    BUCKET?: R2Bucket;
    KEQI_OWNER_ID?: string;
    KEQI_BRIDGE_KEY?: string;
  }
}
