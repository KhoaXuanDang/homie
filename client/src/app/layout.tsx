import type { Metadata, Viewport } from "next"
import { Bricolage_Grotesque, DM_Sans } from "next/font/google"

import { Providers } from "@/components/providers"

import "./globals.css"

const bricolage = Bricolage_Grotesque({
  variable: "--font-bricolage",
  axes: ["opsz"],
  subsets: ["latin"],
})

const dmSans = DM_Sans({
  variable: "--font-dm-sans",
  subsets: ["latin"],
})

export const metadata: Metadata = {
  title: "homie",
  description: "See and control what your Alexa remembers.",
}

export const viewport: Viewport = {
  themeColor: "#183429",
  viewportFit: "cover",
}

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${bricolage.variable} ${dmSans.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <Providers>{children}</Providers>
      </body>
    </html>
  )
}
