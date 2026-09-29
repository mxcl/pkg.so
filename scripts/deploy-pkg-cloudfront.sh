#!/usr/bin/env bash

set -euo pipefail

domain="${PKG_CF_DOMAIN:-pkg.so}"
origin_domain="${PKG_CF_ORIGIN_DOMAIN:-origin.pkg.so}"
origin_header_name="${PKG_CF_ORIGIN_HEADER_NAME:-${AV_WEB_ORIGIN_HEADER:-X-Automic-Vault-Origin}}"
origin_secret="${PKG_CF_ORIGIN_HEADER_VALUE:-${AV_WEB_ORIGIN_SECRET:-}}"
distribution_id="${PKG_CF_DISTRIBUTION_ID:-}"
certificate_arn="${PKG_CF_CERTIFICATE_ARN:-}"
cache_policy_id="${PKG_CF_CACHE_POLICY_ID:-}"
search_cache_policy_id="${PKG_CF_SEARCH_CACHE_POLICY_ID:-}"
headers_policy_id="${PKG_CF_HEADERS_POLICY_ID:-}"
cache_policy_name="pkg-so-daily-cache"
search_cache_policy_name="pkg-so-search-daily-cache"
headers_policy_name="pkg-so-security-headers"
comment="pkg.so package catalog"
prepare_only=false

usage() {
  cat <<EOF
Usage: scripts/deploy-pkg-cloudfront.sh [--prepare-only]

Create or update the pkg.so CloudFront distribution in front of the Atlas
package origin. The script never changes DNS. Until the ACM certificate is
validated, CloudFront is deployed on its generated cloudfront.net hostname.

Required environment:
  AV_WEB_ORIGIN_SECRET          Atlas origin shared secret

Optional environment:
  PKG_CF_DISTRIBUTION_ID        Existing distribution to update
  PKG_CF_CERTIFICATE_ARN        Issued us-east-1 ACM certificate for pkg.so
  PKG_CF_ORIGIN_DOMAIN          Default: ${origin_domain}
  PKG_CF_ORIGIN_HEADER_NAME     Default: ${origin_header_name}
  PKG_CF_ORIGIN_HEADER_VALUE    Overrides AV_WEB_ORIGIN_SECRET
  PKG_CF_CACHE_POLICY_ID        Use an existing cache policy instead
  PKG_CF_SEARCH_CACHE_POLICY_ID Use an existing search cache policy instead
  PKG_CF_HEADERS_POLICY_ID      Use an existing response headers policy instead
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prepare-only)
      prepare_only=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'error: unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

for command in aws jq; do
  command -v "${command}" >/dev/null 2>&1 || {
    printf 'error: missing required command: %s\n' "${command}" >&2
    exit 1
  }
done

if [[ -z "${origin_secret}" ]]; then
  printf 'error: set AV_WEB_ORIGIN_SECRET or PKG_CF_ORIGIN_HEADER_VALUE\n' >&2
  exit 1
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

find_distribution() {
  aws cloudfront list-distributions --output json | jq -r --arg domain "${domain}" --arg comment "${comment}" '
    [.DistributionList.Items[]?
      | select((.Aliases.Items // [] | index($domain)) or .Comment == $comment)
      | .Id][0] // empty
  '
}

find_certificate() {
  aws acm list-certificates --region us-east-1 --output json | jq -r --arg domain "${domain}" '
    [.CertificateSummaryList[]?
      | select(.DomainName == $domain)
      | .CertificateArn][0] // empty
  '
}

certificate_status() {
  aws acm describe-certificate \
    --region us-east-1 \
    --certificate-arn "$1" \
    --query 'Certificate.Status' \
    --output text
}

print_validation_record() {
  local record
  record="$(aws acm describe-certificate \
    --region us-east-1 \
    --certificate-arn "$1" \
    --query 'Certificate.DomainValidationOptions[0].ResourceRecord.[Name,Type,Value]' \
    --output text)"
  if [[ -n "${record}" && "${record}" != "None" ]]; then
    printf 'ACM validation DNS: %s\n' "${record}"
  fi
}

find_cache_policy() {
  aws cloudfront list-cache-policies --type custom --output json | jq -r --arg name "$1" '
    [.CachePolicyList.Items[]?
      | select(.CachePolicy.CachePolicyConfig.Name == $name)
      | .CachePolicy.Id][0] // empty
  '
}

find_headers_policy() {
  aws cloudfront list-response-headers-policies --type custom --output json | jq -r --arg name "$1" '
    [.ResponseHeadersPolicyList.Items[]?
      | select(.ResponseHeadersPolicy.ResponseHeadersPolicyConfig.Name == $name)
      | .ResponseHeadersPolicy.Id][0] // empty
  '
}

ensure_cache_policy() {
  local explicit_id="$1"
  local name="$2"
  local config_file="$3"
  local policy_id etag
  if [[ -n "${explicit_id}" ]]; then
    printf '%s\n' "${explicit_id}"
    return
  fi
  policy_id="$(find_cache_policy "${name}")"
  if [[ "${prepare_only}" == "true" ]]; then
    printf '%s\n' "${policy_id:-will-create:${name}}"
  elif [[ -n "${policy_id}" ]]; then
    etag="$(aws cloudfront get-cache-policy-config --id "${policy_id}" --query ETag --output text)"
    aws cloudfront update-cache-policy \
      --id "${policy_id}" \
      --if-match "${etag}" \
      --cache-policy-config "file://${config_file}" >/dev/null
    printf '%s\n' "${policy_id}"
  else
    aws cloudfront create-cache-policy \
      --cache-policy-config "file://${config_file}" \
      --query 'CachePolicy.Id' \
      --output text
  fi
}

ensure_headers_policy() {
  local explicit_id="$1"
  local name="$2"
  local config_file="$3"
  local policy_id etag
  if [[ -n "${explicit_id}" ]]; then
    printf '%s\n' "${explicit_id}"
    return
  fi
  policy_id="$(find_headers_policy "${name}")"
  if [[ "${prepare_only}" == "true" ]]; then
    printf '%s\n' "${policy_id:-will-create:${name}}"
  elif [[ -n "${policy_id}" ]]; then
    etag="$(aws cloudfront get-response-headers-policy-config --id "${policy_id}" --query ETag --output text)"
    aws cloudfront update-response-headers-policy \
      --id "${policy_id}" \
      --if-match "${etag}" \
      --response-headers-policy-config "file://${config_file}" >/dev/null
    printf '%s\n' "${policy_id}"
  else
    aws cloudfront create-response-headers-policy \
      --response-headers-policy-config "file://${config_file}" \
      --query 'ResponseHeadersPolicy.Id' \
      --output text
  fi
}

if [[ -z "${distribution_id}" ]]; then
  distribution_id="$(find_distribution)"
fi

if [[ -z "${certificate_arn}" ]]; then
  certificate_arn="$(find_certificate)"
fi

if [[ -z "${certificate_arn}" && "${prepare_only}" == "false" ]]; then
  certificate_arn="$(aws acm request-certificate \
    --region us-east-1 \
    --domain-name "${domain}" \
    --validation-method DNS \
    --options CertificateTransparencyLoggingPreference=ENABLED \
    --query CertificateArn \
    --output text)"
  for _attempt in {1..10}; do
    validation_name="$(aws acm describe-certificate \
      --region us-east-1 \
      --certificate-arn "${certificate_arn}" \
      --query 'Certificate.DomainValidationOptions[0].ResourceRecord.Name' \
      --output text)"
    [[ "${validation_name}" != "None" ]] && break
    sleep 1
  done
fi

certificate_is_issued=false
if [[ -n "${certificate_arn}" ]]; then
  if [[ "$(certificate_status "${certificate_arn}")" == "ISSUED" ]]; then
    certificate_is_issued=true
  fi
fi

if [[ "${certificate_is_issued}" == "true" ]]; then
  aliases_json="$(jq -cn --arg domain "${domain}" '{Quantity: 1, Items: [$domain]}')"
  certificate_json="$(jq -cn --arg arn "${certificate_arn}" '{CloudFrontDefaultCertificate: false, ACMCertificateArn: $arn, SSLSupportMethod: "sni-only", MinimumProtocolVersion: "TLSv1.2_2021"}')"
else
  aliases_json='{"Quantity":0}'
  certificate_json='{"CloudFrontDefaultCertificate":true,"MinimumProtocolVersion":"TLSv1"}'
fi

jq -n --arg name "${cache_policy_name}" '{
  Name: $name,
  Comment: "pkg.so package origin cache; CloudFront revalidates with Atlas every five minutes",
  DefaultTTL: 300,
  MaxTTL: 300,
  MinTTL: 0,
  ParametersInCacheKeyAndForwardedToOrigin: {
    EnableAcceptEncodingGzip: true,
    EnableAcceptEncodingBrotli: true,
    HeadersConfig: {HeaderBehavior: "none"},
    CookiesConfig: {CookieBehavior: "none"},
    QueryStringsConfig: {QueryStringBehavior: "none"}
  }
}' >"${tmp_dir}/cache-policy.json"

jq -n --arg name "${search_cache_policy_name}" '{
  Name: $name,
  Comment: "pkg.so search cache with query parameters; revalidates every five minutes",
  DefaultTTL: 300,
  MaxTTL: 300,
  MinTTL: 0,
  ParametersInCacheKeyAndForwardedToOrigin: {
    EnableAcceptEncodingGzip: true,
    EnableAcceptEncodingBrotli: true,
    HeadersConfig: {HeaderBehavior: "none"},
    CookiesConfig: {CookieBehavior: "none"},
    QueryStringsConfig: {
      QueryStringBehavior: "whitelist",
      QueryStrings: {Quantity: 4, Items: ["q", "offset", "limit", "locale"]}
    }
  }
}' >"${tmp_dir}/search-cache-policy.json"

jq -n --arg name "${headers_policy_name}" '{
  Name: $name,
  Comment: "Security headers for pkg.so package pages",
  SecurityHeadersConfig: {
    XSSProtection: {Override: true, Protection: true, ModeBlock: true},
    FrameOptions: {Override: true, FrameOption: "DENY"},
    ReferrerPolicy: {Override: true, ReferrerPolicy: "strict-origin-when-cross-origin"},
    ContentSecurityPolicy: {
      Override: true,
      ContentSecurityPolicy: "default-src '\''self'\''; script-src '\''self'\'' '\''unsafe-inline'\'' https://www.googletagmanager.com https://*.googlesyndication.com https://*.doubleclick.net https://*.google.com https://*.gstatic.com https://*.googletagservices.com; style-src '\''self'\'' '\''unsafe-inline'\'' https://fonts.googleapis.com; font-src '\''self'\'' https://fonts.gstatic.com; img-src '\''self'\'' data: https://*.googlesyndication.com https://*.doubleclick.net https://*.google.com https://*.gstatic.com https://*.googleadservices.com; connect-src '\''self'\'' https://www.google-analytics.com https://*.google.com https://*.googlesyndication.com https://*.doubleclick.net https://*.googleadservices.com; frame-src https://*.googlesyndication.com https://*.doubleclick.net https://*.google.com; frame-ancestors '\''none'\''; base-uri '\''self'\''; form-action '\''none'\''"
    },
    ContentTypeOptions: {Override: true},
    StrictTransportSecurity: {
      Override: true,
      IncludeSubdomains: true,
      Preload: true,
      AccessControlMaxAgeSec: 63072000
    }
  },
  ServerTimingHeadersConfig: {Enabled: false},
  CustomHeadersConfig: {
    Quantity: 1,
    Items: [{
      Header: "Permissions-Policy",
      Value: "camera=(), microphone=(), geolocation=(), payment=()",
      Override: true
    }]
  },
  RemoveHeadersConfig: {Quantity: 0}
}' >"${tmp_dir}/headers-policy.json"

cache_policy_id="$(ensure_cache_policy "${cache_policy_id}" "${cache_policy_name}" "${tmp_dir}/cache-policy.json")"
search_cache_policy_id="$(ensure_cache_policy "${search_cache_policy_id}" "${search_cache_policy_name}" "${tmp_dir}/search-cache-policy.json")"
headers_policy_id="$(ensure_headers_policy "${headers_policy_id}" "${headers_policy_name}" "${tmp_dir}/headers-policy.json")"

origin_json="$(jq -cn \
  --arg domain "${origin_domain}" \
  --arg header_name "${origin_header_name}" \
  --arg header_value "${origin_secret}" \
  '{
    Quantity: 1,
    Items: [{
      Id: "pkg-so-atlas-origin",
      DomainName: $domain,
      OriginPath: "",
      CustomHeaders: {Quantity: 1, Items: [{HeaderName: $header_name, HeaderValue: $header_value}]},
      CustomOriginConfig: {
        HTTPPort: 80,
        HTTPSPort: 443,
        OriginProtocolPolicy: "https-only",
        OriginSslProtocols: {Quantity: 1, Items: ["TLSv1.2"]},
        OriginReadTimeout: 30,
        OriginKeepaliveTimeout: 5
      },
      ConnectionAttempts: 3,
      ConnectionTimeout: 10,
      OriginShield: {Enabled: false},
      OriginAccessControlId: ""
    }]
  }')"

behavior_json() {
  jq -cn \
    --arg cache_policy_id "$1" \
    --arg headers_policy_id "${headers_policy_id}" \
    '{
      TargetOriginId: "pkg-so-atlas-origin",
      TrustedSigners: {Enabled: false, Quantity: 0},
      TrustedKeyGroups: {Enabled: false, Quantity: 0},
      ViewerProtocolPolicy: "redirect-to-https",
      AllowedMethods: {Quantity: 2, Items: ["HEAD", "GET"], CachedMethods: {Quantity: 2, Items: ["HEAD", "GET"]}},
      SmoothStreaming: false,
      Compress: true,
      LambdaFunctionAssociations: {Quantity: 0},
      FunctionAssociations: {Quantity: 0},
      FieldLevelEncryptionId: "",
      CachePolicyId: $cache_policy_id,
      ResponseHeadersPolicyId: $headers_policy_id,
      GrpcConfig: {Enabled: false}
    }'
}

default_behavior_json="$(behavior_json "${cache_policy_id}")"
search_behavior_json="$(behavior_json "${search_cache_policy_id}")"
search_behaviors_json="$(jq -cn \
  --argjson behavior "${search_behavior_json}" \
  '{
    Quantity: 5,
    Items: [
      ($behavior + {PathPattern: "search.json"}),
      ($behavior + {PathPattern: "de/search.json"}),
      ($behavior + {PathPattern: "fr/search.json"}),
      ($behavior + {PathPattern: "ja/search.json"}),
      ($behavior + {PathPattern: "zh-hans/search.json"})
    ]
  }')"

if [[ "${prepare_only}" == "true" ]]; then
  printf 'CloudFront action: %s\n' "$([[ -n "${distribution_id}" ]] && printf update || printf create)"
  printf 'Domain: %s\nOrigin: %s\n' "${domain}" "${origin_domain}"
  printf 'Cache policies: %s, %s\nHeaders policy: %s\n' \
    "${cache_policy_id}" "${search_cache_policy_id}" "${headers_policy_id}"
  if [[ -n "${certificate_arn}" ]]; then
    printf 'Certificate status: %s\n' "$(certificate_status "${certificate_arn}")"
    [[ "${certificate_is_issued}" == "true" ]] || print_validation_record "${certificate_arn}"
  else
    printf 'Certificate status: missing (will request on deploy)\n'
  fi
  exit 0
fi

if [[ -z "${distribution_id}" ]]; then
  jq -n \
    --arg caller_reference "pkg-so-$(date -u +%Y%m%dT%H%M%SZ)" \
    --arg comment "${comment}" \
    --argjson aliases "${aliases_json}" \
    --argjson origins "${origin_json}" \
    --argjson default_behavior "${default_behavior_json}" \
    --argjson search_behaviors "${search_behaviors_json}" \
    --argjson certificate "${certificate_json}" \
    '{
      CallerReference: $caller_reference,
      Aliases: $aliases,
      DefaultRootObject: "",
      Origins: $origins,
      OriginGroups: {Quantity: 0},
      DefaultCacheBehavior: $default_behavior,
      CacheBehaviors: $search_behaviors,
      CustomErrorResponses: {Quantity: 0},
      Comment: $comment,
      Logging: {Enabled: false, IncludeCookies: false, Bucket: "", Prefix: ""},
      PriceClass: "PriceClass_100",
      Enabled: true,
      ViewerCertificate: $certificate,
      Restrictions: {GeoRestriction: {RestrictionType: "none", Quantity: 0}},
      WebACLId: "",
      HttpVersion: "http2and3",
      IsIPV6Enabled: true,
      Staging: false
    }' >"${tmp_dir}/distribution.json"
  aws cloudfront create-distribution \
    --distribution-config "file://${tmp_dir}/distribution.json" \
    --output json >"${tmp_dir}/response.json"
  distribution_id="$(jq -r '.Distribution.Id' "${tmp_dir}/response.json")"
else
  aws cloudfront get-distribution-config \
    --id "${distribution_id}" \
    --output json >"${tmp_dir}/current.json"
  etag="$(jq -r '.ETag' "${tmp_dir}/current.json")"
  jq \
    --arg comment "${comment}" \
    --argjson aliases "${aliases_json}" \
    --argjson origins "${origin_json}" \
    --argjson default_behavior "${default_behavior_json}" \
    --argjson search_behaviors "${search_behaviors_json}" \
    --argjson certificate "${certificate_json}" '
      .DistributionConfig
      | .Aliases = $aliases
      | .DefaultRootObject = ""
      | .Origins = $origins
      | .OriginGroups = {Quantity: 0}
      | .DefaultCacheBehavior = $default_behavior
      | .CacheBehaviors = $search_behaviors
      | .CustomErrorResponses = {Quantity: 0}
      | .Comment = $comment
      | .Enabled = true
      | .PriceClass = "PriceClass_100"
      | .ViewerCertificate = $certificate
      | .HttpVersion = "http2and3"
      | .IsIPV6Enabled = true
    ' "${tmp_dir}/current.json" >"${tmp_dir}/distribution.json"
  aws cloudfront update-distribution \
    --id "${distribution_id}" \
    --if-match "${etag}" \
    --distribution-config "file://${tmp_dir}/distribution.json" \
    --output json >"${tmp_dir}/response.json"
fi

distribution_domain="$(aws cloudfront get-distribution \
  --id "${distribution_id}" \
  --query 'Distribution.DomainName' \
  --output text)"

printf 'CloudFront distribution ID: %s\n' "${distribution_id}"
printf 'CloudFront domain: %s\n' "${distribution_domain}"
if [[ "${certificate_is_issued}" != "true" ]]; then
  printf 'Custom domain status: waiting for ACM DNS validation\n'
  print_validation_record "${certificate_arn}"
  printf 'After validation, rerun this script to attach %s.\n' "${domain}"
fi
