import { createClient } from "@supabase/supabase-js";

// The publishable/anon key only — RLS on xmp_accounts (and its related
// tables) is what actually restricts a signed-in subscriber to their own
// account, not anything client-side. This key is safe to ship to the
// browser precisely because every table it can reach has RLS enabled.
export const supabase = createClient(
  import.meta.env.VITE_SUPABASE_URL as string,
  import.meta.env.VITE_SUPABASE_ANON_KEY as string,
);

export interface OwnedAccount {
  id: string;
  business_name: string;
  category: string | null;
  account_type: string | null;
  reviews: { id: string; rating: number; text: string; date: string; responded: boolean }[];
  listings: { directory: string; name: string; address: string; phone: string }[];
}

// A single embedded ("graph read") query: the subscriber's own account
// joined to its reviews and listings in one round trip. RLS means this can
// only ever return the caller's own account — there is no account_id
// parameter to pass, because the query can't be pointed at anyone else's.
export async function fetchMyAccount(): Promise<OwnedAccount | null> {
  const { data, error } = await supabase
    .from("xmp_accounts")
    .select("id,business_name,category,account_type,reviews:xmp_reviews(id,rating,text,date,responded),listings:xmp_listings(directory,name,address,phone)")
    .maybeSingle();
  if (error) throw error;
  return data as OwnedAccount | null;
}
