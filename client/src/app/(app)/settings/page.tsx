"use client"

import { LogOut } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { useAuth } from "@/lib/auth"

export default function SettingsPage() {
  const auth = useAuth()
  const user = auth.session?.user

  const signOut = async () => {
    try {
      await auth.signOut()
    } catch {
      toast.error("Couldn't sign out. Please try again.")
    }
  }

  return (
    <main className="p-4 md:p-8">
      <Card>
        <CardHeader>
          <CardTitle>Account</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <p className="text-muted-foreground text-sm">Signed in as</p>
            <p className="text-base font-medium">
              {user?.name ?? "Homie user"}
            </p>
            <p className="text-muted-foreground text-sm">{user?.email ?? ""}</p>
          </div>

          <div className="space-y-2 rounded-md border p-4">
            <h2 className="font-medium">Install Homie</h2>
            <p className="text-muted-foreground text-sm">
              iPhone or iPad: open this page in Safari, tap Share, then choose
              Add to Home Screen.
            </p>
            <p className="text-muted-foreground text-sm">
              Android: open this page in Chrome, tap the menu, then choose
              Install app or Add to Home screen.
            </p>
          </div>

          <Button
            variant="destructive"
            size="lg"
            className="w-full justify-center gap-2"
            onClick={() => void signOut()}
          >
            <LogOut className="size-4" aria-hidden />
            Sign out
          </Button>
        </CardContent>
      </Card>
    </main>
  )
}
