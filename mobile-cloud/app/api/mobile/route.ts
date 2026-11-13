import { api, mobileState } from "@/lib/keqi-sync";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return api(() => mobileState(request));
}
