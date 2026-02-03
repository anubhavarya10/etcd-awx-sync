#!/usr/bin/env python3
"""Debug script to test AWX authentication."""

import os
import sys
import requests

AWX_SERVER = os.environ.get("AWX_SERVER", "localhost")
AWX_TOKEN = os.environ.get("AWX_TOKEN")
AWX_CLIENT_ID = os.environ.get("AWX_CLIENT_ID")
AWX_CLIENT_SECRET = os.environ.get("AWX_CLIENT_SECRET")
AWX_USERNAME = os.environ.get("AWX_USERNAME")
AWX_PASSWORD = os.environ.get("AWX_PASSWORD")


def test_connectivity():
    """Test basic connectivity to AWX."""
    print("=" * 60)
    print("AWX Authentication Debug")
    print("=" * 60)

    print(f"\nAWX Server: {AWX_SERVER}")

    # Test basic connectivity
    print("\n[1] Testing connectivity...")
    try:
        response = requests.get(f"http://{AWX_SERVER}/api/v2/", timeout=10)
        print(f"    Status: {response.status_code}")
        if response.status_code == 200:
            print("    ✓ AWX API is reachable")
        else:
            print(f"    ✗ Unexpected status: {response.status_code}")
    except Exception as e:
        print(f"    ✗ Connection failed: {e}")
        return False

    return True


def test_token_auth():
    """Test Personal Access Token authentication."""
    if not AWX_TOKEN:
        print("\n[2] Token Auth: AWX_TOKEN not set, skipping")
        return None

    print("\n[2] Testing Personal Access Token...")
    print(f"    Token (masked): {AWX_TOKEN[:10]}..." if len(AWX_TOKEN) > 10 else f"    Token: {AWX_TOKEN}")

    try:
        headers = {'Authorization': f'Bearer {AWX_TOKEN}'}
        response = requests.get(f"http://{AWX_SERVER}/api/v2/me/", headers=headers, timeout=10)

        if response.status_code == 200:
            user_data = response.json()
            print(f"    ✓ Token valid! User: {user_data.get('username', 'unknown')}")
            return True
        else:
            print(f"    ✗ Token auth failed: {response.status_code}")
            print(f"    Response: {response.text[:200]}")
            return False
    except Exception as e:
        print(f"    ✗ Token auth error: {e}")
        return False


def test_oauth():
    """Test OAuth2 Resource Owner Password authentication."""
    if not all([AWX_CLIENT_ID, AWX_CLIENT_SECRET, AWX_USERNAME, AWX_PASSWORD]):
        print("\n[3] OAuth2 Auth: Not all OAuth env vars set, skipping")
        print(f"    AWX_CLIENT_ID: {'set' if AWX_CLIENT_ID else 'NOT SET'}")
        print(f"    AWX_CLIENT_SECRET: {'set' if AWX_CLIENT_SECRET else 'NOT SET'}")
        print(f"    AWX_USERNAME: {'set' if AWX_USERNAME else 'NOT SET'}")
        print(f"    AWX_PASSWORD: {'set' if AWX_PASSWORD else 'NOT SET'}")
        return None

    print("\n[3] Testing OAuth2 (Resource Owner Password)...")
    print(f"    Client ID: {AWX_CLIENT_ID}")
    print(f"    Client Secret (masked): {AWX_CLIENT_SECRET[:5]}..." if len(AWX_CLIENT_SECRET) > 5 else f"    Client Secret: ***")
    print(f"    Username: {AWX_USERNAME}")

    token_url = f"http://{AWX_SERVER}/api/o/token/"
    print(f"    Token URL: {token_url}")

    data = {
        "grant_type": "password",
        "client_id": AWX_CLIENT_ID,
        "client_secret": AWX_CLIENT_SECRET,
        "username": AWX_USERNAME,
        "password": AWX_PASSWORD
    }

    try:
        response = requests.post(token_url, data=data, timeout=10)

        if response.status_code == 200:
            token_data = response.json()
            access_token = token_data.get("access_token", "")
            print(f"    ✓ OAuth successful! Token: {access_token[:20]}...")
            return True
        else:
            print(f"    ✗ OAuth failed: {response.status_code}")
            print(f"    Response: {response.text}")

            # Provide helpful hints based on error
            if "invalid_client" in response.text:
                print("\n    HINT: 'invalid_client' means:")
                print("      - AWX_CLIENT_ID might not exist in AWX")
                print("      - AWX_CLIENT_SECRET might be wrong")
                print("      - Check AWX -> Settings -> Applications")
                print("      - The OAuth app must have 'Resource owner password-based' grant type")
            elif "invalid_grant" in response.text:
                print("\n    HINT: 'invalid_grant' means:")
                print("      - AWX_USERNAME or AWX_PASSWORD might be wrong")

            return False
    except Exception as e:
        print(f"    ✗ OAuth error: {e}")
        return False


def test_basic_auth():
    """Test basic username/password authentication."""
    if not AWX_USERNAME or not AWX_PASSWORD:
        print("\n[4] Basic Auth: AWX_USERNAME/AWX_PASSWORD not set, skipping")
        return None

    print("\n[4] Testing Basic Authentication...")
    print(f"    Username: {AWX_USERNAME}")

    try:
        response = requests.get(
            f"http://{AWX_SERVER}/api/v2/me/",
            auth=(AWX_USERNAME, AWX_PASSWORD),
            timeout=10
        )

        if response.status_code == 200:
            user_data = response.json()
            print(f"    ✓ Basic auth valid! User: {user_data.get('username', 'unknown')}")
            return True
        else:
            print(f"    ✗ Basic auth failed: {response.status_code}")
            print(f"    Response: {response.text[:200]}")
            return False
    except Exception as e:
        print(f"    ✗ Basic auth error: {e}")
        return False


def main():
    if not test_connectivity():
        print("\n✗ Cannot connect to AWX server. Check AWX_SERVER env var.")
        sys.exit(1)

    results = []

    # Test all auth methods
    token_result = test_token_auth()
    oauth_result = test_oauth()
    basic_result = test_basic_auth()

    # Summary
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)

    working_methods = []

    if token_result:
        working_methods.append("Personal Access Token (AWX_TOKEN)")
    if oauth_result:
        working_methods.append("OAuth2")
    if basic_result:
        working_methods.append("Basic Auth (username/password)")

    if working_methods:
        print(f"\n✓ Working auth methods: {', '.join(working_methods)}")
        print("\nRecommendation:")
        print("  If OAuth is failing but Basic Auth works, you can simplify by:")
        print("  1. Removing AWX_CLIENT_ID and AWX_CLIENT_SECRET from secrets")
        print("  2. The code will automatically fall back to Basic Auth")
    else:
        print("\n✗ No authentication methods working!")
        print("\nTo fix:")
        print("  Option 1 - Use Personal Access Token (simplest):")
        print("    1. Go to AWX -> Users -> your user -> Tokens")
        print("    2. Create a new token")
        print("    3. Set AWX_TOKEN=<the token>")
        print("")
        print("  Option 2 - Use Basic Auth:")
        print("    1. Just set AWX_USERNAME and AWX_PASSWORD")
        print("    2. Remove AWX_CLIENT_ID and AWX_CLIENT_SECRET")
        print("")
        print("  Option 3 - Fix OAuth:")
        print("    1. Go to AWX -> Settings -> Applications")
        print("    2. Create OAuth2 Application with:")
        print("       - Grant type: Resource owner password-based")
        print("       - Client type: Confidential")
        print("    3. Use the Client ID and Client Secret generated")


if __name__ == "__main__":
    main()
