import aiohttp
import asyncio
import traceback
from datetime import datetime
from typing import List, Dict, Optional, Set
import logging

logger = logging.getLogger(__name__)


class AsyncPUBGMatchTracker:
    """Async PUBG API tracker with rate limiting and retry logic"""
    
    def __init__(self, api_key: str, request_delay: float = 7.0, max_retries: int = 3):
        self.api_key = api_key
        self.base_url = "https://api.pubg.com/shards"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/vnd.api+json"
        }
        self.request_delay = request_delay
        self.max_retries = max_retries
        
        self.results = []
        self.request_count = 0
        self.cycle_start_time = None
        self.last_match_ids: Dict[str, str] = {}
        self.processed_matches_this_cycle: Set[str] = set()
        
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def ensure_session(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)
    
    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()
    
    def reset_cycle(self):
        self.results = []
        self.request_count = 0
        self.cycle_start_time = datetime.now()
        self.processed_matches_this_cycle = set()
        logger.info("🔄 Tracker cycle reset")
    
    def check_rate_limit(self, headers: dict):
        limit = headers.get('X-RateLimit-Limit', 'N/A')
        remaining = headers.get('X-RateLimit-Remaining', 'N/A')
        
        if remaining != 'N/A':
            logger.info(f"📊 Rate Limit: {remaining}/{limit} remaining")
            if int(remaining) < 3:
                logger.warning(f"⚠️  WARNING: Only {remaining} requests left!")
    
    async def make_request_with_retry(self, url: str, context: str = "request") -> Optional[dict]:
        await self.ensure_session()
        
        for attempt in range(self.max_retries):
            try:
                self.request_count += 1
                
                async with self.session.get(url, headers=self.headers) as response:
                    self.check_rate_limit(response.headers)
                    
                    if response.status == 429:
                        reset_time = response.headers.get('X-RateLimit-Reset')
                        if reset_time:
                            wait_time = max(int(reset_time) - int(datetime.now().timestamp()) + 2, 60)
                            logger.warning(f"⚠️  Rate limited! Waiting {wait_time}s until reset...")
                        else:
                            wait_time = 60
                            logger.warning(f"⚠️  Rate limited! Waiting {wait_time}s...")
                        
                        await asyncio.sleep(wait_time)
                        continue
                    
                    response.raise_for_status()
                    return await response.json()
                    
            except aiohttp.ClientError as e:
                if attempt == self.max_retries - 1:
                    logger.error(f"❌ Failed {context} after {self.max_retries} attempts: {e}")
                    return None
                    
                wait_time = (attempt + 1) * 5
                logger.warning(f"⚠️  Error on {context}, retrying in {wait_time}s... ({attempt + 1}/{self.max_retries})")
                await asyncio.sleep(wait_time)
            
            except Exception as e:
                logger.error(f"❌ Unexpected error on {context}: {e}")
                return None
        
        return None
    
    async def get_latest_match(
        self, 
        player_name: str, 
        platform: str = "steam", 
        all_tracked_players: Optional[List[str]] = None
    ) -> Optional[dict]:
        player_url = f"{self.base_url}/{platform}/players?filter[playerNames]={player_name}"
        
        try:
            data = await self.make_request_with_retry(player_url, f"player '{player_name}'")
            
            if not data or not data.get('data'):
                logger.warning(f"❌ Player '{player_name}' not found on platform '{platform}'!")
                return None
            
            player = data['data'][0]
            match_ids = [match['id'] for match in player['relationships']['matches']['data']]
            
            if not match_ids:
                logger.warning(f"❌ No matches found for '{player_name}'")
                return None
            
            latest_match_id = match_ids[0]
            
            if latest_match_id in self.processed_matches_this_cycle:
                logger.info(f"✅ Found player: {player['attributes']['name']}")
                logger.info(f"   ⚠️  MATCH ALREADY PROCESSED THIS CYCLE (another tracked player in same game)")
                logger.info(f"   Match ID: {latest_match_id[:16]}...")
                self.last_match_ids[player_name] = latest_match_id
                return None
            
            if player_name in self.last_match_ids:
                if self.last_match_ids[player_name] == latest_match_id:
                    logger.info(f"✅ Found player: {player['attributes']['name']}")
                    logger.info(f"   ⚠️  SAME MATCH as last cycle - SKIPPING!")
                    logger.info(f"   Match ID: {latest_match_id[:16]}...")
                    return None
            
            logger.info(f"✅ Found player: {player['attributes']['name']}")
            logger.info(f"   🆕 NEW MATCH FOUND!")
            logger.info(f"   Match ID: {latest_match_id[:16]}...")
            
            self.processed_matches_this_cycle.add(latest_match_id)
            self.last_match_ids[player_name] = latest_match_id
            
            await asyncio.sleep(self.request_delay)
            
            match_data = await self.get_match_details(
                latest_match_id, 
                platform, 
                all_tracked_players or [player_name]
            )
            
            if match_data:
                self.results.append(match_data)
                logger.info(f"   ✅ Match data saved! (Total: {len(self.results)})")
                return match_data
            
            return None
                
        except Exception as e:
            logger.error(f"❌ Error fetching player '{player_name}': {e}")
            traceback.print_exc()
            return None
    
    async def get_match_details(
        self, 
        match_id: str, 
        platform: str, 
        tracked_players: List[str]
    ) -> Optional[dict]:
        match_url = f"{self.base_url}/{platform}/matches/{match_id}"
        
        try:
            data = await self.make_request_with_retry(match_url, f"match {match_id[:8]}")
            
            if not data:
                return None
            
            match_attrs = data['data']['attributes']
            
            game_mode = match_attrs.get('gameMode', 'Unknown')
            match_type = match_attrs.get('matchType', 'Unknown')
            is_custom = match_attrs.get('isCustomMatch', False)
            map_name = match_attrs.get('mapName', 'Unknown')
            duration = match_attrs.get('duration', 0)
            created_at = match_attrs.get('createdAt', '')
            
            match_category = self.determine_match_category(game_mode, match_type, is_custom)
            
            included = data.get('included', [])
            
            all_players_stats = {}
            
            for player_name in tracked_players:
                stats = self.find_player_stats(included, player_name)
                if stats:
                    all_players_stats[player_name] = stats
                    logger.info(f"   👤 Found stats for: {player_name}")
            
            match_data = {
                "match_id": match_id,
                "match_category": match_category,
                "game_mode": game_mode,
                "match_type": match_type,
                "is_custom": is_custom,
                "map": map_name,
                "duration_seconds": duration,
                "duration_minutes": duration // 60,
                "played_at": created_at,
                "played_at_formatted": self.format_datetime(created_at),
                "all_players_stats": all_players_stats
            }
            
            return match_data
            
        except Exception as e:
            logger.error(f"❌ Error fetching match {match_id}: {e}")
            traceback.print_exc()
            return None
    
    def determine_match_category(self, game_mode: str, match_type: str, is_custom: bool) -> str:
        """
        Determine match category based on game mode and match type
        
        Returns:
        - CUSTOM: Custom matches
        - RANKED: Competitive/ranked matches  
        - CASUAL: Airoyale match type (casual battle royale)
        - NORMAL: Official match type (solo/duo/squad)
        - ARCADE: Arcade/event modes
        """
        if is_custom:
            return "CUSTOM"
        
        game_mode_lower = game_mode.lower()
        match_type_lower = match_type.lower()
        
        # DEBUG: Log the actual values
        logger.info(f"   🔍 DEBUG - game_mode: '{game_mode}' | match_type: '{match_type}'")
        
        # Check for ranked/competitive first
        if 'competitive' in match_type_lower or 'ranked' in match_type_lower:
            logger.info(f"   ✅ Categorized as: RANKED")
            return "RANKED"
        
        # Check for airoyale MATCH TYPE - this is CASUAL
        if 'airoyale' in match_type_lower:
            logger.info(f"   ✅ Categorized as: CASUAL")
            return "CASUAL"
        
        # Check for arcade mode keywords in game_mode
        arcade_keywords = [
            'war', 'zombie', 'training', 'tdm', 'conquest', 'intense', 
            'esports', 'event', 'lab', 'arcade', 'ibr', 'battleroyal'
        ]
        
        if any(keyword in game_mode_lower for keyword in arcade_keywords):
            logger.info(f"   ✅ Categorized as: ARCADE (keyword match)")
            return "ARCADE"
        
        # Normal modes (solo, duo, squad) with official match type = NORMAL
        normal_modes = ['solo', 'solo-fpp', 'duo', 'duo-fpp', 'squad', 'squad-fpp']
        
        if game_mode_lower in normal_modes:
            if match_type_lower in ['official', 'seasonal']:
                logger.info(f"   ✅ Categorized as: NORMAL")
                return "NORMAL"
            else:
                logger.info(f"   ✅ Categorized as: ARCADE (non-official)")
                return "ARCADE"
        
        logger.info(f"   ⚠️  Categorized as: UNKNOWN")
        return f"UNKNOWN ({game_mode})"
    
    def find_player_stats(self, included: list, player_name: str) -> Optional[dict]:
        for item in included:
            if item['type'] == 'participant':
                stats = item['attributes']['stats']
                if stats.get('name', '').lower() == player_name.lower():
                    survival_seconds = stats.get('timeSurvived', 0)
                    survival_minutes = round(survival_seconds / 60, 2)
                    
                    return {
    "rank": stats.get('winPlace', 'N/A'),
    "kills": stats.get('kills', 0),
    "damage_dealt": round(stats.get('damageDealt', 0), 2),
    "assists": stats.get('assists', 0),
    "dbnos": stats.get('DBNOs', 0),
    "headshot_kills": stats.get('headshotKills', 0),
    "longest_kill": round(stats.get('longestKill', 0), 2),
    "revives": stats.get('revives', 0),
    "revives_received": stats.get('revivedCount', 0),
    "team_kills": stats.get('teamKills', 0),
    "vehicle_destroys": stats.get('vehicleDestroys', 0),
    "weapons_acquired": stats.get('weaponsAcquired', 0),
    "boosts_used": stats.get('boosts', 0),
    "heals_used": stats.get('heals', 0),
    "walk_distance": round(stats.get('walkDistance', 0), 2),
    "ride_distance": round(stats.get('rideDistance', 0), 2),
    "swim_distance": round(stats.get('swimDistance', 0), 2),
    "survival_time_minutes": survival_minutes,
    "death_type": stats.get('deathType', 'N/A'),
    "kill_streaks": stats.get('killStreaks', 0),
    "road_kills": stats.get('roadKills', 0)
}
        return None
    
    def format_datetime(self, datetime_str: str) -> str:
        try:
            dt = datetime.fromisoformat(datetime_str.replace('Z', '+00:00'))
            return dt.strftime('%Y-%m-%d %H:%M:%S UTC')
        except:
            return datetime_str
    
    def print_cycle_summary(self, cycle_number: int):
        if self.cycle_start_time:
            elapsed = (datetime.now() - self.cycle_start_time).total_seconds()
        else:
            elapsed = 0
        
        logger.info(f"\n{'='*80}")
        logger.info(f"CYCLE #{cycle_number} SUMMARY")
        logger.info(f"{'='*80}")
        logger.info(f"New Matches: {len(self.results)}")
        logger.info(f"API Requests: {self.request_count}")
        logger.info(f"Cycle Time: {int(elapsed)}s ({elapsed/60:.1f} minutes)")
        logger.info(f"{'='*80}")
        
        if self.results:
            logger.info("\n🆕 NEW Matches Found:")
            for idx, match in enumerate(self.results, 1):
                all_stats = match.get('all_players_stats', {})
                players_list = ', '.join(all_stats.keys())
                total_kills = sum(s.get('kills', 0) for s in all_stats.values())
                best_rank = min(s.get('rank', 99) for s in all_stats.values()) if all_stats else 'N/A'
                logger.info(f"  {idx}. Players: {players_list}")
                logger.info(f"      Best Rank: #{best_rank} | Total Kills: {total_kills}")
        else:
            logger.info("\n⚠️  No new matches found this cycle")