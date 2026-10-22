# Six-channel laboratory build

Base sources: [osqzss/gps-sdr-sim](https://github.com/osqzss/gps-sdr-sim).
See LICENSE for upstream terms. The modifications are maintained in this copy.

Changes:

- Six channels, required by `-N 6`; only healthy visible satellites allocate.
- Abort rendering if fewer than six channels remain available.
- Full 25-frame subframe 4/5 page selection with almanac, health and UTC data.
- Almanac parameters propagated to a common reference epoch; unknown satellites
  emit dummy almanac records with valid parity and unavailable health.
- Preserve ionosphere/UTC page separately from the current paged subframe.
- Keep upstream per-channel C/A, pseudorange, carrier/code Doppler and parity.
- Require valid ionosphere/UTC headers for the full navigation profile.
- Round rewritten ephemeris time to the nearest two-hour boundary, instead of
  flooring it outside the parser's one-hour ephemeris validity window.
- Shift and quantize the UTC polynomial reference consistently with -T.
- Bound ephemeris refresh lookup by the number of loaded sets.
- Exact static-duration sample count and newline-delimited progress/PRN reports.

Reference: IS-GPS-200, sections 20.3.2, 20.3.3 and 20.3.5.
Reserved page content is zero-filled; NMCT is unavailable; special message blank.
This profile is not a certification of compliance with every GPS requirement.
