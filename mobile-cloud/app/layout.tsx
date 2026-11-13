import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "课期 · 手机安排",
  description: "查看与修改作业、考试和课程安排，Mac 恢复运行后同步。",
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
