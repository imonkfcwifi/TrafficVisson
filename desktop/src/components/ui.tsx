import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from "react";
import { Check, LoaderCircle } from "lucide-react";

export function GlassPanel({
  className = "",
  children,
  ...props
}: HTMLAttributes<HTMLElement> & { children: ReactNode }) {
  return (
    <section className={`glass-panel ${className}`} {...props}>
      {children}
    </section>
  );
}

export function PanelHeader({
  eyebrow,
  title,
  action,
}: {
  eyebrow?: string;
  title: string;
  action?: ReactNode;
}) {
  return (
    <header className="panel-header">
      <div>
        {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
        <h2>{title}</h2>
      </div>
      {action ? <div className="panel-action">{action}</div> : null}
    </header>
  );
}

export function Button({
  variant = "secondary",
  className = "",
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger" | "positive";
}) {
  return (
    <button className={`button button--${variant} ${className}`} {...props}>
      {children}
    </button>
  );
}

export function IconButton({
  label,
  className = "",
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  label: string;
  children: ReactNode;
}) {
  return (
    <button
      className={`icon-button ${className}`}
      aria-label={label}
      title={label}
      {...props}
    >
      {children}
    </button>
  );
}

export function StatusDot({
  tone = "success",
  children,
}: {
  tone?: "success" | "warning" | "danger" | "neutral" | "accent";
  children: ReactNode;
}) {
  return (
    <span className={`status-dot status-dot--${tone}`}>
      <span aria-hidden="true" />
      {children}
    </span>
  );
}

export function Badge({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "accent" | "success" | "warning" | "danger";
  children: ReactNode;
}) {
  return <span className={`badge badge--${tone}`}>{children}</span>;
}

export function ProgressBar({
  value,
  tone = "accent",
  label,
}: {
  value: number;
  tone?: "accent" | "success" | "warning" | "danger";
  label?: string;
}) {
  const bounded = Math.max(0, Math.min(100, value));
  return (
    <div
      className={`progress progress--${tone}`}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={bounded}
    >
      <span style={{ width: `${bounded}%` }} />
    </div>
  );
}

export function StepState({
  state,
}: {
  state: "done" | "active" | "waiting";
}) {
  if (state === "done") {
    return (
      <span className="step-state step-state--done">
        <Check size={13} strokeWidth={2.5} aria-hidden="true" /> 완료
      </span>
    );
  }

  if (state === "active") {
    return (
      <span className="step-state step-state--active">
        <LoaderCircle className="spin" size={13} aria-hidden="true" /> 진행 중
      </span>
    );
  }

  return <span className="step-state">대기</span>;
}

export function DefinitionList({
  rows,
}: {
  rows: Array<{ label: string; value: ReactNode }>;
}) {
  return (
    <dl className="definition-list">
      {rows.map((row) => (
        <div key={row.label}>
          <dt>{row.label}</dt>
          <dd>{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}
