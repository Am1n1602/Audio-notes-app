import { ButtonLink, Notice } from "@/components/ui";

export default function NotFound() {
  return (
    <Notice
      tone="neutral"
      title="There's no page here"
      actions={
        <ButtonLink href="/" variant="primary">
          Go to your recordings
        </ButtonLink>
      }
    >
      <p>The address may be mistyped, or the page may have moved.</p>
    </Notice>
  );
}
