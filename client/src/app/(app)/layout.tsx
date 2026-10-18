import { AppShell } from "@/components/app-shell"
import { RequireAuth } from "@/components/require-auth"

export default function AppLayout({ children }: LayoutProps<"/">) {
  return (
    <RequireAuth>
      <AppShell>{children}</AppShell>
    </RequireAuth>
  )
}
