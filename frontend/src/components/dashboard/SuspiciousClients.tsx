import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

/**
 * Dashboard widget: Suspicious clients list.
 * Renders a fixed-height card with a table (framework only; data/columns TBD).
 */
export default function SuspiciousClients() {
  return (
    <Card className="flex h-full min-h-[320px] flex-col gap-2">
      <CardHeader className="shrink-0 pb-0">
        <CardTitle className="text-lg">可疑客户</CardTitle>
      </CardHeader>
      <CardContent className="min-h-0 flex-1 overflow-hidden">
        <div className="h-[260px] overflow-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>客户ID</TableHead>
                <TableHead>备注/原因</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {/* Placeholder row; replace with API data later */}
              <TableRow>
                <TableCell colSpan={2} className="text-muted-foreground text-center py-8">
                  Table content — coming soon
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
