// The components and the order things happen in. The numbers are a real sequence (they match the list beside it).
// Drawn with the page's own colours, so it follows light and dark mode.

function Box({ x, y, h = 52, title, note }: { x: number; y: number; h?: number; title: string; note: string }) {
  return (
    <g>
      <rect x={x} y={y} width={150} height={h} rx={6} fill="var(--sheet)" stroke="var(--rule)" strokeWidth={1.5} />
      <text x={x + 75} y={y + h / 2 - 3} textAnchor="middle" fontSize={14} fontWeight={600} fill="var(--ink)">
        {title}
      </text>
      <text x={x + 75} y={y + h / 2 + 14} textAnchor="middle" fontSize={11.5} fill="var(--ink-soft)">
        {note}
      </text>
    </g>
  );
}

function Step({ x, y, n }: { x: number; y: number; n: number }) {
  return (
    <g>
      <circle cx={x} cy={y} r={9} fill="var(--ink)" />
      <text x={x} y={y + 4} textAnchor="middle" fontSize={11} fontWeight={700} fill="var(--paper)">
        {n}
      </text>
    </g>
  );
}

const arrow = { stroke: "var(--ink-soft)", strokeWidth: 1.5, fill: "none", markerEnd: "url(#head)" } as const;

export function ArchitectureDiagram() {
  return (
    <div className="-mx-5 overflow-x-auto px-5">
      <svg
        viewBox="0 0 640 344"
        role="img"
        aria-labelledby="diagram-title diagram-desc"
        className="h-auto min-w-[600px] w-full"
        fontFamily="var(--font-sans)"
      >
        <title id="diagram-title">How a recording moves through the system</title>
        <desc id="diagram-desc">
          The browser starts an upload with the API, sends the file straight to storage, and finishes the upload with
          the API. The API queues a job; a worker has Gnani transcribe the file from a signed storage link and a
          language model write the summary. The API keeps the job&apos;s state in Postgres and the browser polls it.
        </desc>
        <defs>
          <marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="var(--ink-soft)" />
          </marker>
        </defs>

        <Box x={16} y={24} h={72} title="Browser" note="the web app" />
        <Box x={245} y={24} h={72} title="API" note="checks, records, answers" />
        <Box x={474} y={24} h={72} title="Postgres" note="every job's state" />
        <Box x={16} y={166} title="Storage" note="private bucket" />
        <Box x={245} y={116} title="Queue" note="jobs waiting" />
        <Box x={245} y={216} title="Worker" note="one short step at a time" />
        <Box x={474} y={166} title="Gnani" note="speech to text" />
        <Box x={474} y={266} title="Groq" note="writes the summary" />

        {/* browser <-> API */}
        <path d="M166,40 H243" {...arrow} />
        <Step x={204} y={40} n={1} />
        <path d="M166,62 H243" {...arrow} />
        <Step x={204} y={62} n={3} />
        <path d="M243,84 H168" {...arrow} strokeDasharray="4 3" />
        <Step x={204} y={84} n={8} />
        {/* browser -> storage */}
        <path d="M91,96 V164" {...arrow} />
        <Step x={91} y={130} n={2} />
        {/* api <-> postgres */}
        <path d="M395,60 H472" {...arrow} markerStart="url(#head)" />
        {/* api -> queue -> worker */}
        <path d="M320,96 V114" {...arrow} />
        <Step x={320} y={105} n={4} />
        <path d="M320,168 V214" {...arrow} />
        <Step x={320} y={180} n={5} />
        {/* worker -> gnani, groq */}
        <path d="M395,236 L472,204" {...arrow} />
        <Step x={433} y={220} n={6} />
        <path d="M395,254 L472,286" {...arrow} />
        <Step x={433} y={270} n={7} />
        {/* gnani fetches the file itself from a signed link */}
        <path d="M472,196 H168" {...arrow} strokeDasharray="4 3" />
        <text x={206} y={189} textAnchor="middle" fontSize={11.5} fill="var(--ink-soft)">
          signed link
        </text>
      </svg>
    </div>
  );
}
