"""Offline notebook-equivalent calibration diagnostics with thread-safe figures."""
import numpy as np
import eom_calibration_core as cal


def render(grid, selected, full_table, holdout_table, output, dpi, check=lambda: None):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    def subplots(*args, **kwargs):
        fig = Figure(figsize=kwargs.pop("figsize"), constrained_layout=kwargs.pop("constrained_layout", True))
        FigureCanvasAgg(fig)
        return fig, fig.subplots(*args, **kwargs)
    names = iter(["measured_surfaces", "total_rate_diagnostic", "heldout_check", "selected_matrix", "local_response"])
    def save(fig):
        check()
        name = next(names)
        status = ""
        if name == "heldout_check":
            passed = grid.exact_counts and holdout_table.interval_within_tolerance.all()
            status = f"Held-out check: {'PASS' if passed else 'NOT VALIDATED'} · "
        fig.suptitle(status + "EOM calibration — physical state names provisional", fontsize=10)
        for extension in ("png", "pdf"):
            fig.savefig(output / f"{name}.{extension}", dpi=dpi)
        fig.clear()
    rates = grid.counts / grid.seconds[..., None]
    total = grid.counts.sum(axis=2)
    fraction = np.divide(grid.counts[..., 0], total, out=np.full(total.shape, np.nan), where=total > 0)
    state_names = ["H (S0)", "R (S1)", "V (S2)", "L (S3)"]
    voltage_labels = [f"{s}\n{v:+.3f} V" for s, v in zip(state_names, selected["alice_v"])]
    fig, axes = subplots(1, 3, figsize=(14, 4), constrained_layout=True)
    for ax, z, title, cmap, bounds in zip(
            axes, [rates[..., 0], rates[..., 1], fraction],
            ["CH1 rate", "CH2 rate", "Measured CH1 fraction"],
            ["Blues", "Oranges", "coolwarm"], [{}, {}, {"vmin": 0, "vmax": 1}]):
        im = ax.pcolormesh(grid.alice, grid.bob, z, shading="nearest", cmap=cmap, **bounds)
        fig.colorbar(im, ax=ax)
        ax.set(xlabel="Alice EOM target (V)", ylabel="Bob EOM target (V)", title=title)
    save(fig)
    fig, ax = subplots(figsize=(11, 2.7), constrained_layout=True)
    ax.plot(np.arange(len(grid.points)), grid.points.Rate0_Hz + grid.points.Rate1_Hz, lw=.8)
    ax.set(xlabel="Acquisition row (scan order)", ylabel="CH1 + CH2 (counts/s)",
           title="Total-rate diagnostic: drift and transmission changes are not separated")
    save(fig)

    fig, axes = subplots(1, 2, figsize=(12, 4), constrained_layout=True)
    for b, ax in enumerate(axes):
        tab = holdout_table[holdout_table.bob_setting == b+1]
        y = tab.measured_ch1.to_numpy()
        lo, hi = tab.ci_low.to_numpy(), tab.ci_high.to_numpy()
        ax.errorbar(np.arange(4), y, yerr=[y-lo, hi-y], fmt="o", capsize=4,
                    color=["#0072B2", "#CC4968"][b], label="Held-out measured fraction")
        ax.scatter(np.arange(4), tab.target_ch1, marker="x", s=70, color="black", label="Target")
        for i, row in enumerate(tab.itertuples()):
            ax.fill_between([i-.22, i+.22], row.allowed_low, row.allowed_high, color="gray", alpha=.16)
        ax.set(xticks=np.arange(4), xticklabels=voltage_labels, ylim=(-.04, 1.04),
               ylabel="CH1 / (CH1 + CH2)", title=f"Bob setting {b+1}: {selected['bob_v'][b]:+.3f} V")
        ax.grid(axis="y", alpha=.18)
    axes[0].legend(fontsize=8)
    save(fig)

    # Conditional probability matrix, with confirmed expected unity on diagonal.
    q = full_table.measured_ch1.to_numpy().reshape(4, 2)
    observed = np.column_stack([1-q[:, 0], q[:, 0], 1-q[:, 1], q[:, 1]])[[0, 2, 1, 3]]
    fig, ax = subplots(figsize=(6.8, 5.4), constrained_layout=True)
    im = ax.imshow(observed, vmin=0, vmax=1, cmap="Blues")
    labels = [voltage_labels[i] for i in [0, 2, 1, 3]]
    outputs = [f"B{b+1}/CH{ch}\n{selected['bob_v'][b]:+.3f} V" for b, ch in [(0,2),(0,1),(1,2),(1,1)]]
    ax.set(xticks=range(4), xticklabels=outputs,
           yticks=range(4), yticklabels=labels, xlabel="Output channel", ylabel="Alice role",
           title="Selected measured-grid voltages: full-data descriptive matrix")
    for (i, j), value in np.ndenumerate(observed):
        ax.text(j, i, f"{value:.3f}", ha="center", va="center",
                color="white" if value > .6 else "black")
    fig.colorbar(im, ax=ax, label="Conditional detected fraction")
    save(fig)

    fig, axes = subplots(2, 1, figsize=(11, 6), sharex=True, constrained_layout=True)
    state_colors = ["#0072B2", "#009E73", "#E69F00", "#CC4968"]
    for b, ax in enumerate(axes):
        bi = selected["bob_indices"][b]
        p, lo, hi = cal.wilson(grid.counts[bi, :, 0], grid.counts[bi, :, 1])
        ax.plot(grid.alice, p, ".-", color="#333333", lw=.8, ms=3, label="Measured fraction")
        ax.fill_between(grid.alice, lo, hi, color="gray", alpha=.18, label="Pointwise 95% interval (descriptive)")
        ax.axhline(.5, color="gray", ls="--", lw=.8)
        for s, ai in enumerate(selected["alice_indices"]):
            ax.scatter(grid.alice[ai], p[ai], color=state_colors[s], s=55, zorder=4)
            ax.axvline(grid.alice[ai], color=state_colors[s], alpha=.3, lw=.8)
            ax.text(grid.alice[ai], 1.03, state_names[s], color=state_colors[s], ha="center", fontsize=9)
        ax.set(ylim=(-.03, 1.1), ylabel="CH1 fraction",
               title=f"Bob setting {b+1}: {grid.bob[bi]:+.3f} V")
    axes[-1].set_xlabel("Alice EOM target (V)")
    axes[0].legend(fontsize=8, loc="lower right")
    save(fig)
