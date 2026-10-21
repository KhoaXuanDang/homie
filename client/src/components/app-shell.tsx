"use client"

import { Brain, HouseHeart, LogOut, Settings } from "lucide-react"
import Link from "next/link"
import { usePathname } from "next/navigation"
import { toast } from "sonner"

import { initials, useAuth } from "@/lib/auth"
import { cn } from "@/lib/utils"

const NAV = [
  { href: "/memories", label: "Memories", short: "Memories", icon: Brain },
  { href: "/settings", label: "Settings", short: "Settings", icon: Settings },
]

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const { session, signOut } = useAuth()
  const user = session?.user
  const handleSignOut = () => {
    void signOut().catch(() =>
      toast.error("Couldn't sign out. Please try again."),
    )
  }
  const links = NAV.map((item) => ({
    ...item,
    active: pathname.startsWith(item.href),
  }))

  return (
    <div className="flex min-h-dvh">
      <aside className="bg-sidebar sticky top-0 hidden h-dvh w-60 shrink-0 flex-col gap-6 px-5 py-7 md:flex">
        <Link
          href="/memories"
          className="font-heading flex items-center gap-2.5 text-2xl font-bold text-white"
        >
          <HouseHeart className="text-sidebar-primary size-7" aria-hidden />
          homie
        </Link>
        <p className="text-[13px] text-[#b7cabf]">
          Your everyday memory, close at hand.
        </p>
        <nav aria-label="Main" className="flex flex-col gap-2">
          {links.map(({ href, label, icon: Icon, active }) => (
            <Link
              key={href}
              href={href}
              aria-current={active ? "page" : undefined}
              className={cn(
                "focus-visible:ring-sidebar-ring flex h-11.5 items-center gap-3 rounded-lg px-3.5 text-sm font-medium outline-none focus-visible:ring-2",
                active
                  ? "bg-sidebar-accent font-semibold text-white"
                  : "text-sidebar-foreground hover:bg-sidebar-accent/60",
              )}
            >
              <Icon
                className={cn(
                  "size-[19px]",
                  active ? "text-white" : "text-[#afc2b7]",
                )}
                aria-hidden
              />
              {label}
            </Link>
          ))}
        </nav>
        {user && (
          <div className="border-sidebar-border mt-auto flex items-center gap-2.5 border-t pt-3.5">
            <span
              className="bg-sidebar-primary text-sidebar grid size-9 shrink-0 place-items-center rounded-full text-xs font-bold"
              aria-hidden
            >
              {initials(user.name)}
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-[13px] font-semibold text-white">
                {user.name}
              </p>
              <p className="truncate text-[11px] text-[#afc2b7]">
                {user.email}
              </p>
            </div>
            <button
              type="button"
              onClick={handleSignOut}
              aria-label="Sign out"
              className="focus-visible:ring-sidebar-ring grid size-11 place-items-center rounded-lg text-[#afc2b7] outline-none hover:text-white focus-visible:ring-2"
            >
              <LogOut className="size-[18px]" aria-hidden />
            </button>
          </div>
        )}
      </aside>

      <main className="flex min-w-0 flex-1 flex-col pb-[calc(4rem+env(safe-area-inset-bottom))] md:pb-0">
        {children}
      </main>

      <nav
        aria-label="Main"
        className="bg-card fixed inset-x-0 bottom-0 z-20 flex border-t px-3 pt-2 pb-[max(0.5rem,env(safe-area-inset-bottom))] md:hidden"
      >
        {links.map(({ href, short, icon: Icon, active }) => (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex min-h-14 flex-1 flex-col items-center justify-center gap-1 text-[10px]",
              active
                ? "text-accent-dark font-bold"
                : "font-medium text-[#829088]",
            )}
          >
            <Icon className="size-5" aria-hidden />
            {short}
          </Link>
        ))}
      </nav>
    </div>
  )
}
