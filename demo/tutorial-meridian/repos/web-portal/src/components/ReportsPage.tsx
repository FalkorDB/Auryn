import React, { useEffect, useState } from "react";
import { fetchDailyReport } from "../api/client";

export function ReportsPage() {
  const [report, setReport] = useState<any>(null);
  useEffect(() => {
    fetchDailyReport().then((r) => setReport(r.data.report));
  }, []);
  return <pre>{JSON.stringify(report, null, 2)}</pre>;
}
