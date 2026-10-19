import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SmartOS onboarding",
  description: "Upload sổ Excel và xem các record sẽ được thêm vào database.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="vi">
      <body>{children}</body>
    </html>
  );
}
