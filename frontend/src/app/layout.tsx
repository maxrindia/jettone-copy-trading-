import type { Metadata } from "next";
import "@fontsource-variable/inter";
import "./globals.css";

export const metadata: Metadata = {
  title: "JETTONE | Copy Trading Terminal",
  description: "Automated 1 Master to 5 Followers Copy Trading Engine",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="font-sans antialiased bg-[#F7F7F7] text-[#111111]">
        {children}
      </body>
    </html>
  );
}
