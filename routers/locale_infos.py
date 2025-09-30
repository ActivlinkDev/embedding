@router.get("/locale_infos", response_model=LocaleInfoResponse)
async def locale_infos(
    api_locale: Optional[str] = Query(None, description="Filter by API_Locale (e.g. en_GB)"),
    _: None = Depends(verify_token)
):
    """
    Fetch locale-infos from Strapi.
    Supports optional filtering by API_Locale.
    """
    params = {}
    if api_locale:
        # Use $eq operator so Strapi applies the filter
        params["filters[API_Locale][$eq]"] = api_locale

    headers = {"Authorization": f"Bearer {STRAPI_BEARER_TOKEN}"}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(STRAPI_BASE_URL, params=params, headers=headers, timeout=15.0)

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=f"Strapi error: {response.text}")

        return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
