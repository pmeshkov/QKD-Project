"""Joint physical-response fit and minimum-norm periodic voltage calibration.

All counts enter a shared-phase sin²/cos² Poisson fit. No count splitting,
grid-point selection or nearest-neighbour substitution of fitted settings.
"""
import itertools
import math
import numpy as np
import pandas as pd
from scipy.optimize import least_squares, minimize
from scipy.special import xlogy
from scipy.stats import chi2

from eom_calibration_core import TARGET, STATES

PARAMETERS = ("k_alice_rad_per_v", "k_bob_rad_per_v", "phase_rad",
              "amplitude_ch1_hz", "background_ch1_hz", "amplitude_ch2_hz", "background_ch2_hz")


def check_stop(stop):
    if stop is not None and stop.is_set():
        raise KeyboardInterrupt


def rates(parameters, alice, bob):
    ka, kb, phase, a1, c1, a2, c2 = np.asarray(parameters, float)
    theta = ka*np.asarray(alice) + kb*np.asarray(bob) + phase
    u = np.sin(theta)**2
    return np.stack((c1+a1*u, c2+a2*(1-u)), axis=-1)


def fraction(parameters, theta):
    _, _, _, a1, c1, a2, c2 = np.asarray(parameters, float)
    u = np.sin(theta)**2
    return (c1+a1*u)/(c1+c2+a1*u+a2*(1-u))


def deviance_residual(observed, expected):
    expected = np.maximum(expected, 1e-12)
    d = 2*(expected-observed + xlogy(observed, observed/expected))
    return np.sign(expected-observed)*np.sqrt(np.maximum(d, 0))


def _frequency_starts(grid, kmin, kmax):
    """FFT only initializes the nonlinear fit; voltages still come from CSV columns."""
    starts = []
    da, db = np.diff(grid.alice), np.diff(grid.bob)
    if np.allclose(da, da[0]) and np.allclose(db, db[0]):
        raw = grid.counts/grid.seconds[..., None]
        signal = raw[..., 0]/max(raw[..., 0].mean(), 1e-12)-raw[..., 1]/max(raw[..., 1].mean(), 1e-12)
        signal -= signal.mean()
        size = tuple(2**int(np.ceil(np.log2(n*4))) for n in signal.shape)
        power = np.abs(np.fft.fft2(signal, s=size))**2
        aa = np.pi*np.fft.fftfreq(size[1], da[0])
        bb = np.pi*np.fft.fftfreq(size[0], db[0])
        allowed = ((aa[None, :] >= kmin) & (aa[None, :] <= kmax)
                   & (np.abs(bb[:, None]) >= kmin) & (np.abs(bb[:, None]) <= kmax))
        for flat in np.argsort(np.where(allowed, power, -1).ravel())[::-1]:
            bi, ai = np.unravel_index(flat, power.shape)
            if not allowed[bi, ai]:
                break
            pair = (aa[ai], bb[bi])
            if all(np.linalg.norm(np.array(pair)-p) > .002 for p in starts):
                starts.append(pair)
            if len(starts) == 10:
                break
    # Additional physically broad starts for nonuniform grids / weak FFT peaks.
    ks = np.clip(np.pi/(2*np.array([100., 200., 300.])), kmin*1.001, kmax*.999)
    starts += [(a, sign*b) for a, b, sign in itertools.product(ks, ks, (-1, 1))]
    return starts


def fit_response(grid, vpi_min=50., vpi_max=400., stop=None):
    if not 0 < vpi_min < vpi_max or min(len(grid.alice), len(grid.bob)) < 6:
        raise ValueError("Fit requires at least six distinct voltages on each EOM and 0 < Vpi min < max.")
    if max(np.max(np.diff(grid.alice)), np.max(np.diff(grid.bob))) >= vpi_min:
        raise ValueError("Vpi lower bound must exceed the largest sweep spacing to avoid unresolved fringes.")
    kmin, kmax = np.pi/(2*vpi_max), np.pi/(2*vpi_min)
    x, y = np.meshgrid(grid.alice, grid.bob)
    x, y, dwell = x.ravel()/100, y.ravel()/100, grid.seconds.ravel()
    observed = grid.counts.reshape(-1, 2).astype(float)
    raw_rates = observed/dwell[:, None]
    scale = float(raw_rates.mean())
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("No photons available for a response fit.")
    norm_rates = raw_rates/scale

    def prediction(p, jacobian=False):
        theta = p[0]*x+p[1]*y+p[2]
        u, du = np.sin(theta)**2, np.sin(2*theta)
        expected = np.column_stack((p[4]+p[3]*u, p[6]+p[5]*(1-u)))*dwell[:, None]*scale
        if not jacobian:
            return expected
        j = np.zeros((len(x), 2, 7))
        for col, axis in enumerate((x, y, np.ones(len(x)))):
            j[:, 0, col], j[:, 1, col] = p[3]*du*axis, -p[5]*du*axis
        j[:, 0, 3], j[:, 0, 4] = u, 1
        j[:, 1, 5], j[:, 1, 6] = 1-u, 1
        return j*(dwell*scale)[:, None, None]

    def residual(p):
        check_stop(stop)
        return deviance_residual(observed, prediction(p)).ravel()

    def jac(p):
        mu = np.maximum(prediction(p), 1e-12)
        r = deviance_residual(observed, mu)
        factor = np.divide(1-observed/mu, r, out=1/np.sqrt(mu), where=np.abs(r) > 1e-5)
        return (prediction(p, True)*factor[..., None]).reshape(-1, 7)

    solutions = []
    for ka, kb in _frequency_starts(grid, kmin, kmax):
        check_stop(stop)
        angle = 2*(ka*100*x+kb*100*y)
        coeff = np.linalg.lstsq(np.column_stack((np.ones(len(x)), np.cos(angle), np.sin(angle))), norm_rates[:, 0], rcond=None)[0]
        phase = .5*np.arctan2(coeff[2], -coeff[1])
        amp = np.maximum(np.ptp(norm_rates, axis=0), .01)
        bg = np.maximum(np.min(norm_rates, axis=0), .001)
        p = np.array([ka*100, kb*100, phase, amp[0], bg[0], amp[1], bg[1]])
        lo = [kmin*100, kmin*100 if kb > 0 else -kmax*100, -4*np.pi, 1e-10, 0, 1e-10, 0]
        hi = [kmax*100, kmax*100 if kb > 0 else -kmin*100, 4*np.pi, np.inf, np.inf, np.inf, np.inf]
        result = least_squares(residual, p, jac=jac, bounds=(lo, hi), x_scale="jac", max_nfev=500,
                               ftol=1e-10, xtol=1e-10, gtol=1e-8)
        if result.success and np.isfinite(result.cost):
            solutions.append(result)
    if not solutions:
        raise ValueError("Joint sinusoidal fit did not converge from any initialization.")
    best = min(solutions, key=lambda r: r.cost)
    conversion = np.array([.01, .01, 1, scale, scale, scale, scale])
    parameters = best.x*conversion
    parameters[2] %= np.pi
    mu = prediction(best.x)
    dof = observed.size-len(parameters)
    rank = int(np.linalg.matrix_rank(best.jac))
    covariance = np.linalg.pinv(best.jac.T@best.jac)*conversion[:, None]*conversion[None, :]
    deviance = float(2*best.cost)
    pvalue = float(chi2.sf(deviance, dof))
    freq_boundary = any(abs(abs(parameters[i])-bound) < 1e-5*(kmax-kmin) for i in (0, 1) for bound in (kmin, kmax))
    low_contrast = any(parameters[a]/max(parameters[a]+2*parameters[c], 1e-12) < .1 for a, c in ((3,4),(5,6)))
    diagnostics = []
    if rank < 7:
        diagnostics.append("Unidentifiable fit: Jacobian rank below seven.")
    if freq_boundary:
        diagnostics.append("Fitted Vpi touches the configured fit range; inspect bounds/aliasing.")
    if low_contrast:
        diagnostics.append("Insufficient sinusoidal modulation contrast.")
    if pvalue < .001:
        diagnostics.append("Poisson goodness-of-fit p < 0.001: inspect residual structure, drift and excess noise; model not validated.")
    r2 = []
    for ch in (0, 1):
        expected_rate = mu[:, ch]/dwell
        denom = np.sum((raw_rates[:, ch]-raw_rates[:, ch].mean())**2)
        r2.append(float(1-np.sum((raw_rates[:, ch]-expected_rate)**2)/denom) if denom else None)
    return dict(model="CH1=C1+A1*sin(theta)^2; CH2=C2+A2*cos(theta)^2; theta=kA*VA+kB*VB+phi",
                method="Joint Poisson maximum likelihood using all recorded counts and dwell times",
                parameter_names=list(PARAMETERS), parameters=parameters.tolist(), covariance=covariance.tolist(),
                covariance_note="Local Poisson/Jacobian approximation; no drift or model-discrepancy uncertainty. Boundary amplitudes/backgrounds need care.",
                vpi_alice_v=float(np.pi/(2*parameters[0])), vpi_bob_v=float(np.pi/(2*abs(parameters[1]))),
                period_alice_v=float(np.pi/parameters[0]), period_bob_v=float(np.pi/abs(parameters[1])),
                deviance=deviance, degrees_of_freedom=dof, reduced_deviance=deviance/dof,
                poisson_gof_p_approx=pvalue, r_squared=r2, jacobian_rank=rank,
                converged_starts=len(solutions), diagnostics=diagnostics,
                fit_identified=not (rank < 7 or freq_boundary or low_contrast),
                poisson_model_check_passed=bool(grid.exact_counts and not diagnostics))


def minimum_norm_equivalent(parameters, theta, delta, alice_bounds, bob_bounds):
    """Enumerate period branches and solve each quadratic gauge interval exactly.

    Preserve all eight predicted fractions. This is not a local bounded search
    through the discontinuities of a wrapped objective.
    """
    ka, kb, phi = parameters[:3]
    al, ah = alice_bounds
    bl, bh = bob_bounds
    candidates = []
    for sign in (1, -1):  # sin²(theta) = sin²(-theta); labels stay provisional.
        t, d = sign*np.asarray(theta), sign*delta
        mlo = math.ceil((min(kb*(bh-bl), kb*(bl-bh))-d)/np.pi)
        mhi = math.floor((max(kb*(bh-bl), kb*(bl-bh))-d)/np.pi)
        for m in range(mlo, mhi+1):
            db = (d+m*np.pi)/kb
            left, right = max(bl, bl-db), min(bh, bh-db)
            if left > right:
                continue
            branches, breaks = [], [left, right]
            for phase in t:
                ext = [(ka*a+kb*b+phi-phase)/np.pi for a,b in itertools.product((al,ah), (left,right))]
                ns = np.arange(math.floor(min(ext))-1, math.ceil(max(ext))+2)
                intercept = (phase-phi+np.pi*ns)/ka
                branches.append(intercept)
                for v in (al, ah, -np.pi/(2*ka), np.pi/(2*ka)):
                    breaks.extend(((intercept-v)*ka/kb).tolist())
            edges = np.unique(np.clip(breaks, left, right))
            # A solution can exist only at a boundary when the allowed voltage
            # span exactly equals the required span. Include singleton intervals.
            intervals = list(zip(edges[:-1], edges[1:])) + [(e,e) for e in edges]
            for low, high in intervals:
                mid = (low+high)/2
                chosen = []
                for intercept in branches:
                    av = intercept-kb/ka*mid
                    valid = (av >= al-1e-9) & (av <= ah+1e-9)
                    if not valid.any():
                        break
                    possibilities = intercept[valid]
                    chosen.append(possibilities[np.argmin((possibilities-kb/ka*mid)**2)])
                if len(chosen) != 4:
                    continue
                chosen = np.array(chosen)
                slope = kb/ka
                b0 = np.clip((slope*chosen.sum()-db)/(4*slope*slope+2), low, high)
                av, bv = chosen-slope*b0, np.array([b0, b0+db])
                if np.any(av < al-1e-7) or np.any(av > ah+1e-7):
                    continue
                candidates.append((float(av@av+bv@bv), av, bv))
    if not candidates:
        raise ValueError("No periodic equivalent of the joint phase solution fits the operating limits and measured voltage range.")
    return min(candidates, key=lambda c: c[0])


def choose_voltages(fit, grid, alice_limit=180., bob_limit=180., balance_tolerance=.05, extinction_tolerance=.15, stop=None):
    if not all(np.isfinite([alice_limit, bob_limit])) or not 0 < min(alice_limit, bob_limit) or max(alice_limit, bob_limit) >= 200:
        raise ValueError("Operating limits must be positive and strictly below 200 EOM target V.")
    p = np.array(fit["parameters"])
    reference = np.arange(4)*np.pi/4
    solutions = []
    def residual(x):
        check_stop(stop)
        return (fraction(p, x[:4,None]+np.array([0., x[4]]))-TARGET).ravel()
    # The canonical phase ordering fixes only the role convention, not voltages.
    bounds = (np.r_[reference-np.pi/4, -np.pi/2], np.r_[reference+np.pi/4, 0.])
    for d in (-np.pi/8, -np.pi/4, -3*np.pi/8):
        for shift in (-.15, 0., .15):
            result = least_squares(residual, np.r_[reference+shift, d], bounds=bounds,
                                   ftol=1e-13, xtol=1e-13, gtol=1e-12, max_nfev=2000)
            if result.success:
                solutions.append(result)
    if not solutions:
        raise ValueError("Joint fraction optimization did not converge.")
    best = min(solutions, key=lambda r: r.cost)
    tolerance = np.where(TARGET == .5, balance_tolerance, extinction_tolerance).ravel()
    phase_solution = best.x
    # A least-squares optimum can narrowly miss one tolerance even when another
    # feasible solution exists. Respect every target, not just their average.
    if np.any(np.abs(residual(phase_solution)) > tolerance):
        feasible = []
        for seed in sorted(solutions, key=lambda r: r.cost)[:3]:
            result = minimize(lambda x: float(residual(x)@residual(x)), seed.x, method="SLSQP",
                              bounds=list(zip(*bounds)), constraints=[dict(type="ineq", fun=lambda x: tolerance-np.abs(residual(x)))],
                              options=dict(maxiter=1000, ftol=1e-13))
            if result.success and np.all(np.abs(residual(result.x)) <= tolerance+1e-9):
                feasible.append(result)
        if feasible:
            phase_solution = min(feasible, key=lambda r: r.fun).x
    al = (max(-alice_limit, float(grid.alice.min())), min(alice_limit, float(grid.alice.max())))
    bl = (max(-bob_limit, float(grid.bob.min())), min(bob_limit, float(grid.bob.max())))
    if al[0] >= al[1] or bl[0] >= bl[1]:
        raise ValueError("Operating limits do not overlap the measured sweep.")
    norm, av, bv = minimum_norm_equivalent(p, phase_solution[:4], phase_solution[4], al, bl)
    predictions = rates(p, av[:, None], bv[None, :])
    q = predictions[..., 0]/predictions.sum(axis=-1)
    tolerance = np.where(TARGET == .5, balance_tolerance, extinction_tolerance)
    return dict(alice_v=av.tolist(), bob_v=bv.tolist(), predicted_ch1=q.tolist(),
                predicted_rates_hz=predictions.tolist(), sum_squared_voltages=norm,
                fraction_rmse=float(np.sqrt(np.mean((q-TARGET)**2))),
                predicted_targets_pass=bool(np.all(np.abs(q-TARGET) <= tolerance+1e-9)),
                operating_bounds=dict(alice=list(al), bob=list(bl)),
                optimization="First minimize unweighted squared fraction error across all eight targets; then minimize sum(V²) across phase-equivalent wraps and common phase allocation. No trade of optical error for lower voltage.",
                physical_labels_confirmed=False)


def prediction_table(selected, balance_tolerance=.05, extinction_tolerance=.15):
    rows = []
    for i,j in itertools.product(range(4), range(2)):
        q = selected["predicted_ch1"][i][j]
        t = TARGET[i,j]
        tol = balance_tolerance if t == .5 else extinction_tolerance
        rows.append(dict(state=STATES[i], bob_setting=j+1, alice_v=selected["alice_v"][i], bob_v=selected["bob_v"][j],
                         target_ch1=t, predicted_ch1=q, absolute_error=abs(q-t),
                         predicted_ch1_hz=selected["predicted_rates_hz"][i][j][0],
                         predicted_ch2_hz=selected["predicted_rates_hz"][i][j][1],
                         allowed_low=max(0,t-tol), allowed_high=min(1,t+tol),
                         prediction_within_tolerance=bool(abs(q-t)<=tol+1e-9),
                         measurement_status="MODEL PREDICTION; exact voltage pair not independently measured"))
    return pd.DataFrame(rows)
