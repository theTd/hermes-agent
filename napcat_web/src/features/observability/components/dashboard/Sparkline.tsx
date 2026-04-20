export function Sparkline({
  data,
  width = 300,
  height = 60,
  color = "#4ade80",
}: {
  data: number[];
  width?: number;
  height?: number;
  color?: string;
}) {
  if (!data.length) {
    return (
      <svg width={width} height={height} className="opacity-30">
        <text x={width / 2} y={height / 2} textAnchor="middle" fill="currentColor" fontSize="10">
          no data
        </text>
      </svg>
    );
  }

  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const range = max - min || 1;
  const padX = 2;
  const padY = 4;
  const plotW = width - padX * 2;
  const plotH = height - padY * 2;

  const points = data.map((v, i) => {
    const x = padX + (i / Math.max(data.length - 1, 1)) * plotW;
    const y = padY + plotH - ((v - min) / range) * plotH;
    return `${x},${y}`;
  }).join(" ");

  return (
    <svg width={width} height={height} className="overflow-visible">
      <polyline
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        points={points}
      />
      {data.map((v, i) => {
        const x = padX + (i / Math.max(data.length - 1, 1)) * plotW;
        const y = padY + plotH - ((v - min) / range) * plotH;
        return (
          <circle
            key={i}
            cx={x}
            cy={y}
            r={1.5}
            fill={color}
            opacity={0.8}
          />
        );
      })}
    </svg>
  );
}
