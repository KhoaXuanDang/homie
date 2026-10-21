"use client"

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

export default function MemoriesPage() {
  return (
    <main className="p-4 md:p-8">
      <Card>
        <CardHeader>
          <CardTitle>Memories</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-muted-foreground text-sm">
            Your remembered items will appear here.
          </p>
        </CardContent>
      </Card>
    </main>
  )
}
