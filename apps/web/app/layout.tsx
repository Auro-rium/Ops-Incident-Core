import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "IncidentOps | Evidence desk",
  description: "Connect to an IncidentOps Core API and investigate engineering evidence.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
