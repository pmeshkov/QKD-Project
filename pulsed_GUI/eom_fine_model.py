"""Local phase refinement conditional on a previously identified global model.

No nearest-point selection, independent Vpi refit, or inference of physical labels.
The local model refits the phase and both detector amplitudes/backgrounds with curve_fit.
"""
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.optimize import curve_fit
from scipy.stats import chi2, norm

from eom_calibration_core import TARGET
from eom_model_calibration import rates, check_stop


def load_prior(path, *, allow_rejected=False):
    if not str(path).strip() or not Path(path).is_file():
        raise ValueError('Select the previous global calibration analysis.json, or click Use latest.')
    path = Path(path).resolve()
    raw = path.read_bytes()
    report = json.loads(raw.decode('utf-8-sig'))
    if (report.get('schema') != 'qkd-eom-model-calibration-v2'
            or report.get('status') != 'completed'
            or not report.get('fit', {}).get('fit_identified')):
        raise ValueError('Select a completed global EOM calibration analysis.json with an identified fit. The rejected-candidate override does not bypass this requirement.')
    if not isinstance(report.get('selected'), dict):
        raise ValueError('The global analysis contains no selected voltage candidate to fine-tune.')
    accepted = bool(report.get('transfer_available'))
    rejection = report.get('rejection_reason') or (None if accepted else 'Global candidate was not accepted for transfer.')
    if not accepted and not allow_rejected:
        raise ValueError('Global voltage candidate was rejected: ' + str(rejection) +
                         ' To fine-tune it anyway, set "Override: allow rejected global voltage candidate" to Yes in Voltages.')
    p = np.asarray(report['fit']['parameters'], float)
    a, b = (np.asarray(report['selected'][role+'_v'], float) for role in ('alice', 'bob'))
    if (p.shape != (7,) or a.shape != (4,) or b.shape != (2,)
            or not np.isfinite(np.r_[p, a, b]).all() or p[0] <= 0 or p[1] == 0
            or np.any(p[3:] < 0) or p[3] <= 0 or p[5] <= 0):
        raise ValueError('Prior model parameters or voltage candidate are invalid.')
    for role, volts in (('alice', a), ('bob', b)):
        lo, hi = report['selected']['operating_bounds'][role]
        if not -200 < lo < hi < 200 or np.any(volts < lo-1e-7) or np.any(volts > hi+1e-7):
            raise ValueError('Prior voltages violate their saved operating margins.')
        if len(set(volts)) != len(volts):
            raise ValueError('Prior voltage settings must be distinct.')
    for key in ('balance_tolerance', 'extinction_tolerance'):
        if not 0 < report['parameters'][key] < .5:
            raise ValueError('Prior fraction tolerances are invalid.')
    return report, dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(),
                        candidate_accepted=accepted, rejection_reason=rejection,
                        rejected_candidate_override=bool(not accepted and allow_rejected))


def bounds(prior, limit):
    return {role: [max(-limit, prior['selected']['operating_bounds'][role][0]),
                   min(limit, prior['selected']['operating_bounds'][role][1])]
            for role in ('alice', 'bob')}


def scan_axes(prior, i, j, step, half_steps, limit, sweep_eom):
    """Sweep one EOM about its prior voltage, holding the other at its pair value."""
    if sweep_eom not in ('alice', 'bob'):
        raise ValueError('Select Alice or Bob as the swept EOM / compensator.')
    allowed = bounds(prior, limit)
    result = {}
    for role, index in (('alice', i), ('bob', j)):
        center = prior['selected'][role+'_v'][index]
        axis = center + step*np.arange(-half_steps, half_steps+1) if role == sweep_eom else np.array([center])
        lo, hi = allowed[role]
        if lo >= hi or axis[0] < lo or axis[-1] > hi:
            raise ValueError(f'{role.title()} scan [{axis[0]:.3f}, {axis[-1]:.3f}] V exceeds '
                             f'allowed [{lo:.3f}, {hi:.3f}] V. Reduce the span or choose another pair.')
        if role != sweep_eom and any(v < lo or v > hi for v in prior['selected'][role+'_v']):
            raise ValueError(f'Unchanged {role.title()} voltages exceed the allowed operating margins.')
        result[role+'_axis'] = axis.tolist()
    # A local calibration should not span a full fringe or alias its curvature.
    p = prior['fit']['parameters']
    if step*abs(p[0 if sweep_eom == 'alice' else 1]) >= np.pi/4:
        raise ValueError('Scan spacing is too coarse relative to the swept EOM\'s prior half-wave voltage.')
    return result


def fit_local(prior, grid, stop=None):
    """One joint weighted curve_fit, seeded by the previous physical fit."""
    check_stop(stop)
    p = np.asarray(prior['fit']['parameters'], dtype=float)
    av, bv = np.meshgrid(grid.alice, grid.bob)
    observed = np.asarray(grid.counts, dtype=float)
    exposure = np.asarray(grid.seconds, dtype=float)[..., None]
    if (not np.isfinite(observed).all() or np.any(observed < 0)
            or not np.isfinite(exposure).all() or np.any(exposure <= 0)):
        raise ValueError('Local fit needs finite nonnegative counts and positive measured dwell times.')
    if observed.sum() <= 0:
        raise ValueError('No photons in local sweep; no phase can be fitted.')
    # One x column per voltage pair; the model returns interleaved CH1/CH2 rates.
    x = np.vstack((av.ravel(), bv.ravel()))
    y = (observed/exposure).ravel()
    if y.size <= 5:
        raise ValueError('Local fit needs more than five detector observations for five fitted parameters.')
    # Approximate Poisson rate uncertainty. The one-count floor avoids zero weights.
    sigma = (np.sqrt(np.maximum(observed, 1.))/exposure).ravel()
    initial = p[2:].copy()  # phi, A1, C1, A2, C2 from the previous calibration.
    bound = np.pi/4
    lower = [p[2]-bound, 0., 0., 0., 0.]
    upper = [p[2]+bound, np.inf, np.inf, np.inf, np.inf]

    def response(x, phi, a1, c1, a2, c2):
        check_stop(stop)
        return rates([p[0], p[1], phi, a1, c1, a2, c2], x[0], x[1]).ravel()

    optimal, covariance = curve_fit(
        response, x, y, p0=initial, sigma=sigma, absolute_sigma=True,
        bounds=(lower, upper), method='trf', x_scale=np.maximum(np.abs(initial), 1.),
        max_nfev=10000)
    check_stop(stop)
    fitted = np.r_[p[:2], optimal]
    residual = (y-response(x, *optimal))/sigma
    statistic = float(np.sum(residual**2))
    dof = int(y.size-len(optimal))
    probability = float(chi2.sf(statistic, dof))
    diagnostics = []
    finite_cov = np.isfinite(covariance).all() and np.all(np.diag(covariance) > 0)
    errors = np.sqrt(np.maximum(np.diag(covariance), 0)) if finite_cov else None
    full_rank = bool(finite_cov and np.linalg.matrix_rank(covariance/np.outer(errors, errors)) == len(optimal))
    delta = float(optimal[0]-p[2])
    interval = [float(delta-norm.ppf(.975)*errors[0]), float(delta+norm.ppf(.975)*errors[0])] if full_rank else None
    identified = bool(interval is not None and interval[0] > -bound and interval[1] < bound)
    if not identified:
        diagnostics.append('curve_fit covariance does not constrain the phase within the local branch. Widen the scan or collect more counts.')
    if probability < .001:
        diagnostics.append('Weighted curve-fit goodness-of-fit p < 0.001: inspect residuals and source drift.')
    names = ['phase_rad', 'amplitude_ch1_hz', 'background_ch1_hz', 'amplitude_ch2_hz', 'background_ch2_hz']
    return dict(parameters=fitted.tolist(), phase_correction_rad=delta,
                prior_phase_rad=float(p[2]), phase_rad=float(optimal[0]),
                vpi_alice_v=float(np.pi/(2*abs(p[0]))), vpi_bob_v=float(np.pi/(2*abs(p[1]))),
                optimizer='scipy.optimize.curve_fit', optimized_parameter_names=names,
                initial_guess=initial.tolist(), optimized_parameters=optimal.tolist(),
                covariance=covariance.tolist() if finite_cov else None,
                parameter_standard_errors=errors.tolist() if errors is not None else None,
                phase_ci95_rad=interval, fit_identified=identified,
                chi_squared=statistic, degrees_of_freedom=dof, reduced_chi_squared=statistic/dof,
                gof_p_approx=probability, diagnostics=diagnostics,
                usable=bool(identified and probability >= .001),
                method='Joint weighted curve_fit of both detector rates; prior Vpi fixed; phase, amplitudes and backgrounds refitted',
                weights='sigma_rate = sqrt(max(observed_counts, 1)) / measured_dwell_s; absolute_sigma=True',
                uncertainty_note='Local Gaussian covariance approximation from curve_fit, conditional on fixed prior Vpi. Not a profile interval or a global ambiguity check; unreliable for sparse counts or parameters at bounds.',
                bounds=dict(lower=lower, phase_upper=float(upper[0]), rate_lower=0.))


def refine_voltages(prior, fit, axes, i, j, limit, sweep_eom):
    """Compensate the combined phase drift using only the swept EOM.

    Apply the nearest feasible common translation to its voltage list, keeping
    the other EOM unchanged and the selected pair inside the measured scan.
    """
    if sweep_eom not in ('alice', 'bob'):
        raise ValueError('Select Alice or Bob as the compensator.')
    p = np.asarray(fit['parameters'])
    ka, kb, phi, a1, c1, a2, c2 = p
    av, bv = (np.asarray(prior['selected'][r+'_v']) for r in ('alice', 'bob'))
    allowed = bounds(prior, limit)
    fixed_role = 'bob' if sweep_eom == 'alice' else 'alice'
    volts, index, slope = (av, i, ka) if sweep_eom == 'alice' else (bv, j, kb)
    fixed, fixed_index = (bv, j) if sweep_eom == 'alice' else (av, i)
    axis = np.asarray(axes[sweep_eom+'_axis'], float)
    if len(axis) < 5 or not np.array_equal(axes[fixed_role+'_axis'], [fixed[fixed_index]]):
        raise ValueError('Compensation requires one swept EOM and the other fixed at its prior pair voltage.')
    fl, fh = allowed[fixed_role]
    if np.any(fixed < fl) or np.any(fixed > fh):
        raise ValueError('Unchanged EOM voltages exceed the operating margins.')
    lo, hi = allowed[sweep_eom]
    # A common shift must keep every role within bounds, not just this pair.
    shifts = [max(lo-volts.min(), axis.min()-volts[index]),
              min(hi-volts.max(), axis.max()-volts[index])]
    if shifts[0] > shifts[1]:
        raise ValueError('No common voltage translation keeps all six values inside the operating margins.')
    target = TARGET[i,j]
    u = 0. if target == 0 else 1. if target == 1 else (c2+a2-c1)/(a1+a2)
    if not 0 <= u <= 1:
        raise ValueError('The fitted detector responses cannot achieve a 50:50 detected split.')
    theta = ka*av[i]+kb*bv[j]+phi
    base = np.arcsin(np.sqrt(u))
    possible_phase = [theta+slope*shift for shift in shifts]
    candidates = []
    for sign in (-1, 1):
        for n in range(int(np.floor((min(possible_phase)-sign*base)/np.pi))-1,
                       int(np.ceil((max(possible_phase)-sign*base)/np.pi))+2):
            shift = float((sign*base+n*np.pi-theta)/slope)
            if not shifts[0]-1e-10 <= shift <= shifts[1]+1e-10:
                continue
            # Remove roundoff at an exact endpoint, without extrapolating.
            candidates.append(float(np.clip(shift, *shifts)))
    if not candidates:
        raise ValueError('The fitted target lies outside the single-EOM scan/operating margins. No extrapolation or boundary substitute was made; widen/recenter or repeat the global sweep.')
    shift = min(candidates, key=abs)
    x, y = (shift, 0.) if sweep_eom == 'alice' else (0., shift)
    aa, bb = av+x, bv+y
    norm = float(aa@aa+bb@bb)
    rr = rates(p, aa[:,None], bb[None,:])
    fractions = rr[...,0]/rr.sum(axis=-1)
    cfg = prior['parameters']
    tolerances = np.where(TARGET == .5, cfg['balance_tolerance'], cfg['extinction_tolerance'])
    return dict(alice_v=aa.tolist(), bob_v=bb.tolist(), predicted_ch1=fractions.tolist(),
                predicted_rates_hz=rr.tolist(), predicted_targets_pass=bool(np.all(abs(fractions-TARGET) <= tolerances)),
                operating_bounds=allowed, sum_squared_voltages=norm,
                fraction_rmse=float(np.sqrt(np.mean((fractions-TARGET)**2))),
                alice_common_shift_v=x, bob_common_shift_v=y, compensation_eom=sweep_eom,
                optimization='Exact target inside the measured single-EOM scan; smallest common voltage shift of the swept EOM. Other EOM unchanged; prior relative phase separations retained.',
                physical_labels_confirmed=False)
