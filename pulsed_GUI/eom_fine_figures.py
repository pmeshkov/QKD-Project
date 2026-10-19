"""Local calibration diagnostics and clearly scoped independent verification."""
import numpy as np
from eom_calibration_figures import figure, save
from eom_calibration_core import TARGET, wilson
from eom_model_calibration import rates, check_stop


def render(grid, report, output, dpi, stop=None):
    fit = report['fit']
    p = np.asarray(fit['parameters'])
    av, bv = np.meshgrid(grid.alice, grid.bob)
    counts, seconds = grid.counts, grid.seconds
    total = counts.sum(axis=-1)
    fractions = np.divide(counts[...,0], total, out=np.full(total.shape, np.nan), where=total>0)
    fig, axes = figure(2, 2, (12, 8))
    ax = axes[0,0]
    selected = report.get('selected')
    i,j = report['parameters']['pair_indices']
    sweep_eom = report['parameters'].get('sweep_eom')
    if sweep_eom:
        voltage = grid.alice if sweep_eom == 'alice' else grid.bob
        fixed_eom = 'bob' if sweep_eom == 'alice' else 'alice'
        fixed_v = float(grid.bob[0] if sweep_eom == 'alice' else grid.alice[0])
        xx = np.linspace(voltage.min(), voltage.max(), 600)
        curves = rates(p, xx, fixed_v) if sweep_eom == 'alice' else rates(p, fixed_v, xx)
        good = total.ravel() > 0
        fraction, lo, hi = wilson(counts[...,0].ravel()[good], counts[...,1].ravel()[good])
        ax.errorbar(voltage[good], fraction, yerr=[np.maximum(0, fraction-lo), np.maximum(0, hi-fraction)],
                    fmt='o', ms=4, capsize=3, color='#0072B2', label='Counts + 95% Wilson interval')
        ax.plot(xx, curves[:,0]/curves.sum(axis=-1), color='#0072B2', label='Fitted response')
        ax.axhline(TARGET[i,j], color='gray', ls='--', label=f'Target {TARGET[i,j]:g}')
        if selected:
            index = i if sweep_eom == 'alice' else j
            ax.axvline(selected[sweep_eom+'_v'][index], color='#D55E00', label='Fitted compensator voltage')
        xlabel = f'{sweep_eom.title()} target voltage (V)'
        ax.set(xlabel=xlabel, ylabel='CH1 / (CH1 + CH2)',
               title=f'S{i} / Bob {j+1} — {fixed_eom.title()} fixed at {fixed_v:+.3f} V')
        ax.legend(fontsize=7)
        measured_x = voltage
    else:
        # Retain honest 2D views for previously acquired mesh data.
        im = ax.pcolormesh(grid.alice, grid.bob, fractions, shading='nearest', cmap='viridis')
        from matplotlib.ticker import ScalarFormatter
        formatter = ScalarFormatter(useOffset=False)
        formatter.set_scientific(False)
        fig.colorbar(im, ax=ax, label='Measured CH1 / total (local min–max scale)', format=formatter)
        if selected:
            ax.scatter(selected['alice_v'][i], selected['bob_v'][j], marker='x', s=90, color='red', label='Continuous fitted target')
            ax.legend(fontsize=8)
        ax.set(xlabel='Alice target (V)', ylabel='Bob target (V)', title=f'Legacy measured mesh — S{i} / Bob {j+1}')
        phase = p[0]*av+p[1]*bv+p[2]
        xx = np.linspace(phase.min(), phase.max(), 600)
        curves = rates(p, (xx-p[2])/p[0], np.zeros_like(xx))
        measured_x, xlabel = phase.ravel(), 'Combined fitted sin² phase (rad)'
    ax = axes[0,1]
    for ch,color in ((0,'#0072B2'), (1,'#D55E00')):
        ax.errorbar(measured_x, (counts[...,ch]/seconds).ravel(),
                    yerr=(np.sqrt(counts[...,ch])/seconds).ravel(), fmt='.', ms=3,
                    alpha=.45, color=color, linewidth=.5)
        ax.plot(xx, curves[:,ch], color=color, label=f'CH{ch+1} fitted rate')
    ax.set(xlabel=xlabel, ylabel='Counts/s', title='Both detector responses (√N error bars)')
    ax.legend(fontsize=8)
    ax = axes[1,0]
    if fit.get('optimizer') == 'scipy.optimize.curve_fit':
        expected = rates(p, av, bv)
        sigma = np.sqrt(np.maximum(counts, 1.))/seconds[..., None]
        residual = (counts/seconds[..., None]-expected)/sigma
        for ch,color in ((0,'#0072B2'),(1,'#D55E00')):
            ax.plot(measured_x, residual[...,ch].ravel(), 'o', ms=4, color=color, label=f'CH{ch+1}')
        ax.axhline(0, color='gray', lw=1)
        ax.set(xlabel=xlabel, ylabel='Residual / estimated counting σ',
               title=f"curve_fit residuals — χ²/dof = {fit['reduced_chi_squared']:.3f}")
        ax.legend(fontsize=8)
    else:
        ax.plot(fit['profile_phase_rad'], np.asarray(fit['profile_deviance'])-fit['deviance'], color='#0072B2')
        ax.axhline(3.8414588, color='gray', ls='--', label='Approx. 95% profile threshold')
        ax.axvline(fit['phase_correction_rad'], color='#D55E00', label='Fitted correction')
        ax.set(xlabel='Phase correction from prior (rad)', ylabel='Deviance above minimum', ylim=(0,12),
               title=f"Fixed prior Vπ; fitted rate scale {fit['brightness_scale']:.3f}")
        if fit['phase_ci95_rad']:
            lo,hi = fit['phase_ci95_rad']; delta = fit['phase_correction_rad']
            ax.set_xlim(max(-np.pi/4,delta-2*(hi-lo)), min(np.pi/4,delta+2*(hi-lo)))
            ax.axvspan(lo,hi,color='gray',alpha=.12)
            ax.ticklabel_format(axis='x',useOffset=False)
        ax.legend(fontsize=8)
    ax = axes[1,1]
    verification = report.get('verification')
    if verification:
        v = verification
        ax.axhspan(v['allowed_low'], v['allowed_high'], color='gray', alpha=.18, label='Accepted fraction range')
        ax.scatter([0], [v['predicted_ch1']], color='#0072B2', label='Model prediction')
        if v['measured_ch1'] is not None:
            value = v['measured_ch1']
            ax.errorbar([1], [value], yerr=[[value-v['ci_low']], [v['ci_high']-value]],
                        fmt='s', capsize=5, color='#D55E00', label=f"Fresh counts + {v['confidence']:.0%} interval")
        else:
            ax.text(.5,.5,'No verification photons', transform=ax.transAxes, ha='center')
        ax.set(xticks=[0,1], xticklabels=['Prediction','Independent check'], ylim=(-.04,1.04), xlim=(-.6,1.6),
               ylabel='CH1 / (CH1 + CH2)',
               title=f"Selected pair only: {'PASS' if v['passed'] else 'NOT PASSED'}; N={sum(v['counts'])}\nAlice {v['alice_v']:+.3f} V; Bob {v['bob_v']:+.3f} V")
        ax.legend(fontsize=7)
    else:
        ax.axis('off')
        ax.text(0, .95, '\n'.join(['Independent verification pending.',
                (f"Weighted χ²/dof: {fit['reduced_chi_squared']:.3f}" if 'reduced_chi_squared' in fit else
                 f"Local deviance/dof: {fit['reduced_deviance']:.3f}"),
                f"Approximate GOF p: {fit.get('gof_p_approx', fit.get('poisson_gof_p_approx')):.4g}",
                *fit['diagnostics']]), va='top', transform=ax.transAxes, wrap=True, fontsize=9)
    title = f'Single-EOM fine-tuning — {sweep_eom.title()} compensator' if sweep_eom else 'Legacy 2D EOM fine-tuning'
    fig.suptitle(title + ' — prior Vπ fixed; other pairs remain predictions')
    save(fig, output, 'fine_tuning', dpi, lambda: check_stop(stop))
