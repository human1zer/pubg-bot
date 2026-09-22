.mode column
.headers on

WITH w AS (
  SELECT * FROM matches
  WHERE played_at >= datetime('now','-30 days')
    AND UPPER(match_category) NOT IN ('CASUAL','ARCADE')
)
SELECT 'suicide'  AS award, player_name, COUNT(*) AS val
FROM w WHERE death_type='suicide' GROUP BY player_name
ORDER BY val DESC LIMIT 3;

WITH w AS (
  SELECT * FROM matches
  WHERE played_at >= datetime('now','-30 days')
    AND UPPER(match_category) NOT IN ('CASUAL','ARCADE')
)
SELECT 'teamkill' AS award, player_name, SUM(team_kills) AS val
FROM w GROUP BY player_name HAVING val > 0
ORDER BY val DESC LIMIT 3;

WITH w AS (
  SELECT * FROM matches
  WHERE played_at >= datetime('now','-30 days')
    AND UPPER(match_category) NOT IN ('CASUAL','ARCADE')
)
SELECT 'roadkill' AS award, player_name, SUM(road_kills) AS val
FROM w GROUP BY player_name HAVING val > 0
ORDER BY val DESC LIMIT 3;

WITH w AS (
  SELECT * FROM matches
  WHERE played_at >= datetime('now','-30 days')
    AND UPPER(match_category) NOT IN ('CASUAL','ARCADE')
)
SELECT 'bluezone' AS award, player_name, COUNT(*) AS val
FROM w WHERE death_type='byzone' GROUP BY player_name
ORDER BY val DESC LIMIT 3;

WITH w AS (
  SELECT * FROM matches
  WHERE played_at >= datetime('now','-30 days')
    AND UPPER(match_category) NOT IN ('CASUAL','ARCADE')
)
SELECT 'worst_kd' AS award, player_name,
       printf('%.2f', CAST(SUM(kills) AS REAL)/COUNT(*)) AS val
FROM w GROUP BY player_name HAVING COUNT(*) >= 5
ORDER BY CAST(SUM(kills) AS REAL)/COUNT(*) ASC LIMIT 3;
