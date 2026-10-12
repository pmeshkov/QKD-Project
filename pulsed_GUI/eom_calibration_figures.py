"""Physical response-fit diagnostics; Agg figures are safe in the GUI worker."""
import numpy as np
from eom_model_calibration import rates, deviance_residual


def figure(rows=1, cols=1, size=(11, 4)):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig = Figure(figsize=size, constrained_layout=True)
    FigureCanvasAgg(fig)
    return fig, fig.subplots(rows, cols, squeeze=False)


def save(fig, output, name, dpi, check):
    check()
    for ext in ('png', 'pdf'):
        fig.savefig(output / f'{name}.{ext}', dpi=dpi)
    fig.clear()


def fractions_plot(selected, table, output, dpi, check=lambda: None, verification=False):
    fig, axes = figure(1, 2, (12, 4.5))
    labels = [f'S{i}\n{v:+.2f} V' for i, v in enumerate(selected['alice_v'])]
    for b, ax in enumerate(axes[0]):
        tab = table[table.bob_setting == b+1]
        ax.scatter(range(4), tab.predicted_ch1, color='#0072B2', label='Fitted prediction', zorder=3)
        ax.scatter(range(4), tab.target_ch1, color='black', marker='x', s=55, label='Target', zorder=3)
        for i, row in enumerate(tab.itertuples()):
            ax.fill_between([i-.23, i+.23], row.allowed_low, row.allowed_high, color='gray', alpha=.18)
        if verification:
            y, lo, hi = tab.measured_ch1.to_numpy(), tab.ci_low.to_numpy(), tab.ci_high.to_numpy()
            ax.errorbar(range(4), y, yerr=[np.maximum(0,y-lo), np.maximum(0,hi-y)], fmt='s', capsize=4,
                        color='#D55E00', label='Independent measurement + intervals')
        ax.set(xticks=range(4), xticklabels=labels, ylim=(-.04, 1.04), ylabel='CH1 / (CH1 + CH2)',
               title=f"Bob setting {b+1}: {selected['bob_v'][b]:+.2f} V")
        ax.grid(axis='y', alpha=.2)
    axes[0,0].legend(fontsize=8, loc='best')
    fig.suptitle('Independent fixed-voltage verification' if verification else
                 'Model predictions at selected voltages — experimental verification required')
    save(fig, output, 'verification' if verification else 'predicted_fractions', dpi, check)


def render(grid, fit, selected, table, output, dpi, check=lambda: None):
    av, bv = np.meshgrid(grid.alice, grid.bob)
    measured = grid.counts/grid.seconds[..., None]
    predicted = rates(fit['parameters'], av, bv)
    residual = deviance_residual(grid.counts, predicted*grid.seconds[..., None])
    fig, axes = figure(2, 3, (13, 7.5))
    for ch in (0, 1):
        high = max(measured[...,ch].max(), predicted[...,ch].max())
        for ax, z, title, bounds, cmap in zip(axes[ch],
                (measured[...,ch], predicted[...,ch], residual[...,ch]),
                ('Measured rate (Hz)', 'Joint fitted rate (Hz)', 'Signed deviance residual'),
                (dict(vmin=0,vmax=high), dict(vmin=0,vmax=high), dict(vmin=-max(3,np.max(abs(residual))),vmax=max(3,np.max(abs(residual))))),
                ('viridis','viridis','RdBu_r')):
            im = ax.pcolormesh(grid.alice, grid.bob, z, shading='nearest', cmap=cmap, **bounds)
            fig.colorbar(im, ax=ax)
            ax.set(xlabel='Alice EOM target (V)', ylabel='Bob EOM target (V)', title=f'CH{ch+1}: {title}')
    fig.suptitle(f"Joint sinusoidal fit — deviance/dof = {fit['reduced_deviance']:.3f}; R² = "
                 + ', '.join('undefined' if r is None else f'{r:.4f}' for r in fit['r_squared']))
    save(fig, output, 'response_fit', dpi, check)
    # Retain acquisition order to expose drift; unequal detector efficiencies can
    # produce real total-rate changes, so overlay the predicted total as well.
    pts = grid.points
    expected = rates(fit['parameters'], pts.EOM0_Target_V.to_numpy(), pts.EOM1_Target_V.to_numpy())
    fig, axes = figure(2, 1, (11, 5.5))
    ax = axes[0,0]
    ax.plot(pts.Rate0_Hz.to_numpy()+pts.Rate1_Hz.to_numpy(), '.', ms=2, color='gray', label='Measured total')
    ax.plot(expected.sum(axis=1), lw=.8, color='#0072B2', label='Fitted total')
    ax.set(ylabel='CH1 + CH2 (Hz)'); ax.legend(fontsize=8)
    ax = axes[1,0]
    for ch in (0,1):
        ax.plot(pts[f'Rate{ch}_Hz'].to_numpy()-expected[:,ch], '.', ms=2, label=f'CH{ch+1}')
    ax.axhline(0, color='black', lw=.6)
    ax.set(xlabel='Acquisition row (scan order)', ylabel='Measured − fitted (Hz)'); ax.legend(fontsize=8)
    fig.suptitle('Scan-order diagnostic — inspect for drift and structured residuals')
    save(fig, output, 'scan_residuals', dpi, check)
    if selected is None:
        return
    fractions_plot(selected, table, output, dpi, check)
    q = np.array(selected['predicted_ch1'])
    matrix = np.column_stack([1-q[:,0],q[:,0],1-q[:,1],q[:,1]])[[0,2,1,3]]
    fig, axes = figure(size=(6.8,5.5)); ax = axes[0,0]
    im = ax.imshow(matrix, vmin=0, vmax=1, cmap='Blues')
    ax.set(xticks=range(4), xticklabels=['B1/CH2','B1/CH1','B2/CH2','B2/CH1'],
           yticks=range(4), yticklabels=[f"S{i}: {selected['alice_v'][i]:+.2f} V" for i in (0,2,1,3)],
           xlabel='Output channel', ylabel='Alice role', title='Predicted matrix — not a measured matrix')
    for (i,j), value in np.ndenumerate(matrix):
        ax.text(j,i,f'{value:.3f}',ha='center',va='center',color='white' if value>.6 else 'black')
    fig.colorbar(im, ax=ax, label='Conditional detected fraction')
    save(fig,output,'predicted_matrix',dpi,check)
    fig, axes = figure(2,1,(11,7))
    x = np.linspace(grid.alice.min(),grid.alice.max(),1000)
    for b, ax in enumerate(axes[:,0]):
        bi = np.argmin(abs(grid.bob-selected['bob_v'][b]))
        for ch, color in ((0,'#0072B2'),(1,'#D55E00')):
            ax.errorbar(grid.alice,measured[bi,:,ch],yerr=np.sqrt(grid.counts[bi,:,ch])/grid.seconds[bi],
                        fmt='.',ms=3,lw=.6,color=color,alpha=.6,label=f'CH{ch+1}: data at Bob {grid.bob[bi]:+.2f} V')
            ax.plot(x,rates(fit['parameters'],x,np.full_like(x,grid.bob[bi]))[:,ch],ls='--',lw=1,color=color,
                    label=f'CH{ch+1}: fit at same measured Bob voltage')
            ax.plot(x,rates(fit['parameters'],x,np.full_like(x,selected['bob_v'][b]))[:,ch],color=color,lw=1.4,
                    label=f'CH{ch+1}: prediction at selected Bob voltage')
        for i,v in enumerate(selected['alice_v']):
            ax.axvline(v,color='gray',lw=.6,alpha=.4)
            ax.text(v,1.01,f'S{i}',transform=ax.get_xaxis_transform(),ha='center',fontsize=8)
        ax.set(ylabel='Count rate (Hz)',xlabel='Alice EOM target (V)',title=f"Selected Bob {b+1}: {selected['bob_v'][b]:+.2f} V")
        ax.legend(fontsize=7,ncol=2)
    fig.suptitle('Detector-response slices — nearby measured slices are not candidate verification')
    save(fig,output,'response_slices',dpi,check)
