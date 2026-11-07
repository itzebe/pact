/**
 * First-run orientation.
 *
 * PACT has no separate marketing page: the product is the first screen, and a
 * guest workspace is provisioned before anything renders. That means the empty
 * workspace *is* the landing page, and it has to answer "what is this and what do
 * I do" in one read. It appears only when the workspace has no settlements yet, so
 * a returning user goes straight to their work. The step-by-step flow already
 * lives in the sidebar explainer, so this panel does not repeat it.
 */
export default function Welcome({ onStart }) {
  return (
    <section className="panel px-5 py-6 sm:px-7 sm:py-8">
      <span className="label">Escrow for agent commerce</span>
      <h1 className="mt-2 max-w-2xl text-xl font-semibold tracking-tight text-paper sm:text-2xl">
        Pay for work only once it is proven.
      </h1>
      <p className="mt-3 max-w-2xl text-sm leading-relaxed text-paper-dim">
        Post a job and seller agents compete on price. PayPal holds the agreed amount
        in escrow, and it is released only after an independent verifier confirms the
        delivery against your acceptance criteria. If the work fails, the hold is
        voided and the buyer is charged nothing.
      </p>

      <div className="mt-5 flex flex-wrap items-center gap-3">
        <button type="button" onClick={onStart} className="btn-primary">
          Post your first job
        </button>
        <span className="text-2xs text-paper-faint">
          No account needed — this is a private guest workspace.
        </span>
      </div>
    </section>
  )
}
