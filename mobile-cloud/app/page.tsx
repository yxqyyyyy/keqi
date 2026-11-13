import { requireChatGPTUser } from "./chatgpt-auth";
import MobileClient from "./mobile-client";

export const dynamic = "force-dynamic";

export default async function Home() {
  await requireChatGPTUser("/");
  return <MobileClient />;
}
