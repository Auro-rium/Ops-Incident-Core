import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "IncidentOps | Deployment paused",
  description: "The IncidentOps demo deployment is temporarily paused.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
