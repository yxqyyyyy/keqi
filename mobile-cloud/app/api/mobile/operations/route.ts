import { addOperation, api } from "@/lib/keqi-sync";

export async function POST(request: Request) {
  return api(() => addOperation(request));
}
