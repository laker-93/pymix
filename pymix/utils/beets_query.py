from typing import List, Union


def or_query(field: str, values: List[Union[str, int]], exact: bool = False) -> List[str]:
    """
    Build a beets query OR-ing ``field:value`` terms (``field::value`` -- beets'
    exact/regex match -- when ``exact`` is set). beets treats a bare comma between
    terms as OR, so ``[A, B, C]`` becomes the argv ``field:A , field:B , field:C``
    (each token is its own argv element -- the comma must not be glued to a term).
    """
    sep = "::" if exact else ":"
    query: List[str] = []
    for i, value in enumerate(values):
        if i:
            query.append(",")
        query.append(f"{field}{sep}{value}")
    return query
