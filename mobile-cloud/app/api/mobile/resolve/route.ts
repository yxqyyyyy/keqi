import { api, resolveOperation } from "@/lib/keqi-sync";

export async function POST(request: Request) {
  return api(() => resolveOperation(request));
}
