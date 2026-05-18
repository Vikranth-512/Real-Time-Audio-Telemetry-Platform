export const evaluateAssumptions = ({ meanRms, trend, variance }) => {
  const assumptions = [];

  const HIGH_RMS_THRESHOLD = 0.5;
  const MODERATE_RMS_THRESHOLD = 0.15;
  const QUIET_RMS_THRESHOLD = 0.05;
  const SILENCE_THRESHOLD = 0.005;

  const HIGH_TREND_THRESHOLD = 0.05;
  const MODERATE_TREND_THRESHOLD = 0.015;

  const HIGH_VARIANCE_THRESHOLD = 0.05;
  const LOW_VARIANCE_THRESHOLD = 0.005;

  // 1. Variation level
  if (variance < LOW_VARIANCE_THRESHOLD) {
      assumptions.push("Low variation");
  } else if (variance < HIGH_VARIANCE_THRESHOLD) {
      assumptions.push("Moderate variation");
  } else {
      assumptions.push("High variation");
  }

  // 2. Noise level
  if (meanRms <= SILENCE_THRESHOLD) {
      assumptions.push("Silent");
  } else if (meanRms <= QUIET_RMS_THRESHOLD) {
      assumptions.push("Quiet");
  } else if (meanRms <= HIGH_RMS_THRESHOLD) {
      assumptions.push("Moderate noise");
  } else {
      assumptions.push("Loud");
  }

  // 3. Signal state
  if (variance > HIGH_VARIANCE_THRESHOLD && Math.abs(trend) > HIGH_TREND_THRESHOLD) {
      assumptions.push("Unstable signal");
  } else if (trend > MODERATE_TREND_THRESHOLD) {
      assumptions.push("Rising signal");
  } else if (trend < -MODERATE_TREND_THRESHOLD) {
      assumptions.push("Falling signal");
  } else {
      assumptions.push("Stable signal");
  }

  return assumptions;
};

export const getTimeContext = () => {
  const hour = new Date().getHours();
  if (hour >= 6 && hour < 12) return "morning";
  if (hour >= 12 && hour < 18) return "afternoon";
  if (hour >= 18 && hour < 22) return "evening";
  return "night";
};
