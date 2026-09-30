"use client";

import { use } from "react";
import { UploadView } from "@/components/upload-view";

export default function UploadPage({ params }: PageProps<"/uploads/[id]">) {
  const { id } = use(params);
  return <UploadView id={id} />;
}
