import { api, bridgeSync } from "@/lib/keqi-sync";

export async function POST(request: Request) {
  return api(() => bridgeSync(request));
}
