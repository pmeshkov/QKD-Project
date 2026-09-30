"""Publication exports; display permutations never alter acquisition arrays."""
import math

import numpy as np

COLORS = ("#0072B2", "#E69F00", "#009E73", "#CC4968")
STATES = ("H", "V", "R", "L")
BASES = ("HV", "RL")


def calibrated_states(settings):
    """Recover role labels from the recorded six-voltage handoff, never counts."""
    import json
    from calibration_transfer import parse
    try:
        candidate = settings["calibration_transfer"]["imported_candidate"]
        candidate = parse(json.dumps(candidate, allow_nan=False))
        if any(not math.isclose(settings["bob"][b], v, rel_tol=0, abs_tol=1e-7)
               for b, v in zip(BASES, candidate["bob_v"])):
            return None
        role_labels = []
        for voltage in candidate["alice_v"]:
            matches = [s for s, v in settings["alice"].items()
                       if math.isclose(v, voltage, rel_tol=0, abs_tol=1e-7)]
            if len(matches) != 1:
                return None
            role_labels.append(matches[0])
        return role_labels if len(set(role_labels)) == 4 else None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def layout(result, diagonal=True):
    """Map the confirmed index calibration or saved physical labels to outcomes.

    Confirmed by the user: S0 -> HV/CH2, S2 -> HV/CH1,
    S1 -> RL/CH2, S3 -> RL/CH1. No inference from measured brightness.
    """
    from polarization_analysis import column
    states = result["states"]
    settings = result["session"]["settings"]
    mapping = settings["mapping"]
    imported_roles = calibrated_states(settings)
    calibration_derived = False
    if set(states) == {"S0", "S1", "S2", "S3"}:
        ordered = ["S0", "S2", "S1", "S3"]
        physical = [("HV", 1), ("HV", 0), ("RL", 1), ("RL", 0)]
        columns = [column(b, ch, mapping)[0] for b, ch in physical]
        provenance = "User-confirmed index calibration: S0=HV/CH2, S2=HV/CH1, S1=RL/CH2, S3=RL/CH1."
    elif set(states) == set(STATES) and all(mapping[b] in b for b in BASES):
        ordered, columns = list(STATES), list(range(4))
        provenance = "Saved physical Alice states and detector outcome assignments."
    elif imported_roles and set(imported_roles) == set(states) and all(mapping[b] == "Unassigned" for b in BASES):
        # Renaming S0/S1/S2/S3 must not erase their calibrated detector roles.
        ordered = [imported_roles[i] for i in (0, 2, 1, 3)]
        columns = [column(b, ch, mapping)[0] for b, ch in (("HV", 1), ("HV", 0), ("RL", 1), ("RL", 0))]
        provenance = ("Recorded six voltages match imported qkd-eom-calibration-transfer-v1 roles: "
                      "entries 0/2 -> Bob1 CH2/CH1; entries 1/3 -> Bob2 CH2/CH1. "
                      "State names are provisional; no brightness-based sorting.")
        calibration_derived = True
    else:
        return dict(rows=list(range(4)), columns=list(range(4)),
                    output_states=[None]*4, expected=[[None]*4 for _ in range(4)],
                    diagonal=False, provenance="Detector/state mapping unassigned; retained recorded order.")
    expected = np.full((4, 4), .5)
    output_states = [None]*4
    for i, state in enumerate(ordered):
        row, col = states.index(state), columns[i]
        expected[row, col] = 1.
        expected[row, columns[i ^ 1]] = 0.
        output_states[col] = state
    rows = [states.index(s) for s in ordered] if diagonal else list(range(4))
    cols = columns if diagonal else list(range(4))
    return dict(rows=rows, columns=cols, output_states=[output_states[c] for c in cols],
                expected=expected[np.ix_(rows, cols)].tolist(), diagonal=diagonal, provenance=provenance,
                calibration_derived=calibration_derived)


def arrival_limits(result, start, end):
    period = min(row["sync_period_ns"] for row in result["rows"])
    end = end or min(period, result["edges"][-1])
    if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end:
        raise ValueError("Arrival plot start must be nonnegative and less than its end (including automatic end).")
    return [float(start), float(end)]


def render(result, output, rebin, plot_stop_ns, plot_y_max=0, diagonal=True, plot_dpi=300,
           provisional_labels=True, plot_start_ns=0):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.colors import to_rgb
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import MaxNLocator

    order = layout(result, diagonal)
    rows, cols = order["rows"], order["columns"]
    settings = result["session"]["settings"]
    states = [result["states"][r] for r in rows]
    aliases = {"S0": "H", "S1": "R", "S2": "V", "S3": "L"} if provisional_labels else {}
    def state_label(state):
        return f"{aliases[state]} ({state})" if state in aliases else state
    gate = result["gate"]
    gate_text = "Ungated" if gate is None else f"Gate: {gate[0]:g}–{gate[1]:g} ns"
    status = f"{result['completed_settings']}/8 settings · {result['session']['status']}"
    if any(s in aliases for s in states) or order.get("calibration_derived"):
        status += " · H/R/V/L names provisional"
    # Colors follow the output state, including when using recorded order.
    canonical = ["S0", "S2", "S1", "S3"] if states[0].startswith("S") else list(STATES)
    colors = [COLORS[canonical.index(s)] if s in canonical else COLORS[i]
              for i, s in enumerate(order["output_states"])]
    row_colors = [COLORS[canonical.index(s)] if s in canonical else "#30343B" for s in states]
    output_labels = []
    for i, col in enumerate(cols):
        basis = BASES[col // 2]
        label = result["labels"][col]
        state = order["output_states"][i]
        name = f"{state_label(state)} · {label}" if state and (state.startswith("S") or order.get("calibration_derived")) else label
        output_labels.append(f"{name}\nBob {settings['bob'][basis]:+g} V")
    input_labels = [f"{state_label(s)}\nAlice {settings['alice'][s]:+g} V" for s in states]

    def save(fig, name):
        # Vector curves/text for publication; PNG for the experiment GUI.
        for ext in ("png", "pdf", "svg"):
            fig.savefig(output / f"{name}.{ext}", dpi=plot_dpi, facecolor="white")
        fig.clear()

    fig = Figure(figsize=(10.4, 8.4), facecolor="white")
    FigureCanvasAgg(fig)
    axes = fig.subplots(4, 4, sharex=True, sharey=True,
                        gridspec_kw={"hspace": 0, "wspace": 0})
    fig.subplots_adjust(left=.15, right=.98, bottom=.13, top=.83)
    edges = result["edges"][::rebin]
    start, limit = arrival_limits(result, plot_start_ns, plot_stop_ns)
    visible = (edges[:-1] < limit) & (edges[1:] > start)
    selected_bins = np.flatnonzero(visible)
    peak = 0
    for i, row in enumerate(rows):
        for j, col in enumerate(cols):
            ax = axes[i, j]
            if result["available"][row, col]:
                hist = result["histograms"][row, col].reshape(-1, rebin).sum(axis=1)
                peak = max(peak, int(hist[visible].max(initial=0)))
                # Drawing only intersecting bins keeps native-resolution vector
                # exports compact. Full native histograms remain saved separately.
                if selected_bins.size:
                    first, last = selected_bins[0], selected_bins[-1]
                    drawn, drawn_edges = hist[first:last+1], edges[first:last+2]
                    ax.stairs(drawn, drawn_edges, fill=True, color=colors[j], alpha=.5, linewidth=0)
                    ax.stairs(drawn, drawn_edges, color=colors[j], linewidth=.9)
            else:
                ax.text(.5, .5, "Not acquired", transform=ax.transAxes, ha="center", color="#777777", fontsize=8)
            if gate:
                ax.axvspan(*gate, color="#444444", alpha=.06, zorder=0)
                for edge in gate:
                    if start < edge < limit:
                        ax.axvline(edge, color="#777777", linewidth=.6, linestyle=(0, (3, 3)), zorder=0)
            if i == 0:
                ax.set_title(output_labels[j], color=colors[j], fontsize=10, pad=12)
            if j == 0:
                ax.set_ylabel(input_labels[i], color=row_colors[i], fontsize=10, labelpad=15)
            ax.tick_params(labelsize=8, direction="out", length=3, width=.7, pad=4)
            ax.xaxis.set_major_locator(MaxNLocator(nbins=4, prune="upper"))
            ax.yaxis.set_major_locator(MaxNLocator(nbins=3, integer=True, prune="upper"))
            for spine in ax.spines.values():
                spine.set_linewidth(.8)
                spine.set_color("#30343B")
            ax.label_outer()
    axes[0, 0].set_xlim(start, limit)
    # Leave a label-width margin at each panel's right edge so adjacent panels'
    # end/start ticks do not collide in the gapless grid.
    ticks = MaxNLocator(nbins=4).tick_values(start, limit)
    axes[0, 0].set_xticks(ticks[(ticks >= start) & (ticks <= start + .82 * (limit-start))])
    axes[0, 0].set_ylim(0, plot_y_max or max(1, 1.08*peak))
    fig.text(.565, .972, "Polarization-resolved photon arrival", ha="center", fontsize=15)
    fig.text(.565, .924, "Output channel", ha="center", fontsize=11)
    fig.text(.565, .077, "Time after SYNC (ns)", ha="center", fontsize=11)
    fig.text(.025, .48, f"Detected counts / {edges[1]-edges[0]:g} ns bin", va="center", rotation=90, fontsize=11)
    arrival_gate = "No arrival gate selected" if gate is None else f"{gate_text} (shading only)"
    fig.text(.565, .034, f"Color identifies output channel · {arrival_gate} · {status}", ha="center", fontsize=8, color="#555555")
    fig.text(.565, .014, "Labels show EOM target voltages: Alice/AO0 and Bob/AO1. Histograms retain measured counts.", ha="center", fontsize=8, color="#555555")
    save(fig, "arrival_histograms")

    fig = Figure(figsize=(12.6, 6.5), facecolor="white")
    FigureCanvasAgg(fig)
    axes = fig.subplots(1, 2)
    fig.subplots_adjust(left=.115, right=.98, bottom=.22, top=.82, wspace=.33)
    for ax, key, title in zip(axes, ("rates", "probabilities"), ("Detected rate (counts/s)", "Conditional detection probability")):
        matrix = result[key][np.ix_(rows, cols)]
        finite = matrix[np.isfinite(matrix)]
        scale = 1 if key == "probabilities" else max(1, float(finite.max()) if len(finite) else 1)
        image = np.ones((4, 4, 3))
        for i in range(4):
            for j in range(4):
                value = matrix[i, j]
                if math.isfinite(value):
                    strength = .08 + .62 * np.clip(value/scale, 0, 1)
                    image[i, j] = 1 - strength * (1 - np.array(to_rgb(colors[j])))
                else:
                    image[i, j] = [.94, .94, .94]
                if order["expected"][i][j] == 1:
                    ax.add_patch(Rectangle((j-.49, i-.49), .98, .98, fill=False,
                                           edgecolor=colors[j], linewidth=2.0))
                ax.text(j, i, "N/A" if not math.isfinite(value) else
                        f"{value:.3f}" if key == "probabilities" else f"{value:.1f}",
                        ha="center", va="center", fontsize=12, color="#18212B")
        ax.imshow(image, interpolation="nearest", zorder=0)
        ax.set_xticks(range(4), output_labels, fontsize=8)
        ax.set_yticks(range(4), input_labels, fontsize=9)
        for tick, color in zip(ax.get_xticklabels(), colors):
            tick.set_color(color)
        for tick, color in zip(ax.get_yticklabels(), row_colors):
            tick.set_color(color)
        ax.set_xticks(np.arange(-.5, 4, 1), minor=True)
        ax.set_yticks(np.arange(-.5, 4, 1), minor=True)
        ax.grid(which="minor", color="white", linewidth=1)
        ax.tick_params(which="both", length=0, pad=9)
        ax.set_title(title, fontsize=12, pad=15)
        ax.set_xlabel("Output channel / Bob EOM target", fontsize=10, labelpad=12)
        for spine in ax.spines.values():
            spine.set_visible(False)
    fig.text(.55, .95, "Polarization transfer matrix", ha="center", fontsize=16)
    fig.text(.55, .89, f"{gate_text} · {status}", ha="center", fontsize=10, color="#555555")
    fig.text(.015, .52, "Input state / Alice EOM target", va="center", rotation=90, fontsize=10)
    fig.text(.55, .077, "Color identifies output channel; shade and numbers show measured values. Outlines mark expected unity outcomes.", ha="center", fontsize=8)
    fig.text(.55, .045, "Probabilities are normalized within each Bob basis. No background subtraction or detector-efficiency correction.", ha="center", fontsize=8, color="#555555")
    fig.text(.55, .018, "EOM targets: Alice/AO0, Bob/AO1. Plot order is saved in analysis.json; raw data and CSV matrix order are unchanged.", ha="center", fontsize=8, color="#555555")
    save(fig, "polarization_matrix")
