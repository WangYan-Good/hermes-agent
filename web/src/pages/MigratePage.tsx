import { Archive, Server } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { useI18n } from "@/i18n";

/**
 * Backup/migration hub. Two sections: local backup & restore (moved here
 * from SystemPage in a follow-up task) and migrating the whole instance to
 * another host. Both are placeholders for now — the real panels land next.
 */
export default function MigratePage() {
  const { t } = useI18n();

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader className="border-b border-border bg-card">
          <div className="flex items-center gap-2">
            <Archive className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">Backup &amp; restore</CardTitle>
          </div>
          <CardDescription>
            Create a full backup of this instance, or restore from a
            previously created one.
          </CardDescription>
        </CardHeader>
        <CardContent className="py-8 text-center text-sm text-muted-foreground">
          Backup &amp; restore controls are coming here.
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="border-b border-border bg-card">
          <div className="flex items-center gap-2">
            <Server className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">{t.migration.title}</CardTitle>
          </div>
          <CardDescription>{t.migration.description}</CardDescription>
        </CardHeader>
        <CardContent className="py-8 text-center text-sm text-muted-foreground">
          Migration controls are coming here.
        </CardContent>
      </Card>
    </div>
  );
}
