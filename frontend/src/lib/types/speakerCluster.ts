/**
 * TypeScript types for speaker clustering and global speaker management.
 */

interface GenderComposition {
  male_count: number;
  female_count: number;
  unknown_count: number;
  total_with_gender: number;
  dominant_gender: string | null;
  gender_coherence: number | null;
  gender_label: string | null;
  has_gender_conflict: boolean;
}

export interface SpeakerCluster {
  uuid: string;
  label: string | null;
  description: string | null;
  user_id: number;
  member_count: number;
  promoted_to_profile_id: number | null;
  promoted_to_profile_uuid: string | null;
  promoted_to_profile_name: string | null;
  promoted_to_profile_avatar_url: string | null;
  suggested_name: string | null;
  quality_score: number | null;
  min_similarity: number | null;
  separation_score: number | null;
  gender_composition: GenderComposition | null;
  created_at: string;
  updated_at: string;
}

export interface SpeakerClusterMember {
  uuid: string;
  speaker_uuid: string;
  speaker_name: string;
  display_name: string | null;
  suggested_name: string | null;
  media_file_uuid: string | null;
  media_file_title: string | null;
  confidence: number;
  margin: number | null;
  verified: boolean;
  predicted_gender: string | null;
  predicted_age_range: string | null;
  gender_confidence: number | null;
  gender_confirmed_by_user: boolean;
  has_audio_clip: boolean;
  created_at: string | null;
}

export interface SpeakerClusterDetail extends SpeakerCluster {
  members: SpeakerClusterMember[];
}

export interface SpeakerInboxItem {
  speaker_uuid: string;
  speaker_name: string;
  display_name: string | null;
  suggested_name: string | null;
  suggestion_source: string | null;
  confidence: number | null;
  media_file_uuid: string | null;
  media_file_title: string | null;
  media_file_duration: number | null;
  cluster_uuid: string | null;
  cluster_label: string | null;
  cluster_member_count: number;
  verified: boolean;
  predicted_gender: string | null;
  predicted_age_range: string | null;
  created_at: string | null;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  per_page: number;
  pages: number;
  labeled_count?: number;
  unlabeled_count?: number;
  last_clustered_at?: string | null;
}

export interface BatchVerifyRequest {
  speaker_uuids: string[];
  action: 'accept' | 'assign' | 'name' | 'skip';
  profile_uuid?: string;
  display_name?: string;
}

export interface BatchVerifyResponse {
  updated_count: number;
  failed_count: number;
  errors: string[];
}

export interface ReclusterResponse {
  status: string;
  task_id: string | null;
  message: string;
}

export interface MinorityAnalysisItem {
  speaker_uuid: string;
  speaker_name: string;
  predicted_gender: string;
  sim_to_centroid: number;
  avg_sim_to_majority: number;
  avg_sim_to_minority_peers: number | null;
  outlier_score: number;
  recommendation: 'likely_outlier' | 'borderline' | 'likely_valid';
}

export interface OutlierAnalysisResponse {
  cluster_uuid: string;
  majority_gender: string;
  minority_gender: string;
  minority_analysis: MinorityAnalysisItem[];
}

export interface ClusterUnassignRequest {
  speaker_uuids: string[];
  blacklist: boolean;
}

export interface SpeakerProfile {
  uuid: string;
  name: string;
  description: string | null;
  embedding_count: number;
  instance_count?: number;
  media_count?: number;
  predicted_gender: string | null;
  source_cluster_id: number | null;
  created_at: string;
  updated_at: string;
  avatar_url?: string | null;
  is_shared?: boolean;
  owner_name?: string | null;
}
