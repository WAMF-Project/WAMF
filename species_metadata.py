import requests


def fetch_species_metadata(
    scientific_name
):

    summary_response = requests.get(
    f"https://en.wikipedia.org/api/rest_v1/page/summary/{scientific_name.replace(' ', '_')}",
    headers={
        "User-Agent": (
            "WAMF/1.0 "
            "(Bird Wildlife Monitoring)"
        )
    },
    timeout=10
)

    

    summary_response.raise_for_status()

    summary_data = summary_response.json()
   

    wikipedia_url = (
    summary_data["content_urls"]
    ["desktop"]
    ["page"]
)


    return {
        "scientific_name": scientific_name,
        "description": summary_data["extract"],
        "wikipedia_url": wikipedia_url
    }