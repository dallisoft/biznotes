import { NextResponse } from "next/server";

// Compose healthcheck / K8s probe 공용 엔드포인트
export function GET() {
  return NextResponse.json({ status: "ok" });
}
